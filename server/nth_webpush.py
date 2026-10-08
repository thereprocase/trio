"""Web Push for the nth dashboard: encryption, VAPID, subscriptions, delivery.

A phone that installs the dashboard as a web app can receive notifications
while the page is closed. The browser hands us a *subscription* (an endpoint
URL on its vendor's push service plus two keys); we encrypt each notification
to those keys and POST the ciphertext to the endpoint. The push service only
ever relays ciphertext.

Standards implemented here, with no dependency beyond `cryptography` (already
present in the hub venv, pulled in by the MCP SDK):

  * RFC 8291 -- message encryption (aes128gcm content coding, ECDH P-256,
    HKDF-SHA-256). Proven against the RFC's Appendix A vector in
    tests/test-webpush.py.
  * RFC 8292 -- VAPID: an ES256 JWT identifying this server to the push
    service, sent as `Authorization: vapid t=<jwt>, k=<public key>`.
  * RFC 8030 -- the delivery request itself (TTL and Urgency headers).

Everything that decides WHETHER to notify is a pure function (`decide`,
`flush_due`, `endpoint_allowed`) so it can be tested without a network, a
database or the cryptography package. When `cryptography` is missing the
module still imports; `available()` reports False and the dashboard serves the
page without push.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from nth_constants import can_see, parse_recipients

try:
    from cryptography.exceptions import InvalidSignature  # noqa: F401  (re-exported for tests)
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature, encode_dss_signature)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - exercised only on installs without it
    HAVE_CRYPTO = False


def available() -> bool:
    """True when this process can encrypt and sign pushes."""
    return HAVE_CRYPTO


# ───────── Constants ─────────

PUSH_MODES = ("all", "mentions", "every5m", "off")
# What a device may subscribe with. Off is expressed by unsubscribing, so no
# row exists that can never deliver yet still counts against a quota.
SUBSCRIBE_MODES = ("all", "mentions", "every5m")
DIGEST_INTERVAL_S = 300          # every5m: at most one notification per window
DEFAULT_CONTACT = "mailto:admin@example.com"
VAPID_KEY_FILENAME = "push-vapid-key.pem"
JWT_LIFETIME_S = 12 * 3600       # RFC 8292 caps exp at 24h; 12h leaves margin
JWT_REFRESH_MARGIN_S = 3600
PUSH_TTL_S = 12 * 3600           # how long a push service holds an undelivered push
SEND_TIMEOUT_S = 10
RECORD_SIZE = 4096
# RFC 8030 push services accept at most 4096 octets of body. The aes128gcm
# header is 86 octets (16 salt + 4 record size + 1 key length + 65 key), and
# the record adds a 16-octet tag and the 0x02 delimiter, leaving this much
# for the JSON payload.
MAX_PUSH_BODY = 4096
HEADER_LEN = 16 + 4 + 1 + 65
MAX_PLAINTEXT = MAX_PUSH_BODY - HEADER_LEN - 16 - 1     # 3993
MAX_ENDPOINT_LEN = 1024
MAX_BODY_CHARS = 240
MAX_TITLE_CHARS = 120
# What a notification says in place of the message when the device has not
# opted in to showing message text. A lock screen is readable by anyone
# holding the phone, so the text stays inside the app unless asked for.
HIDDEN_BODY = "New message"
# A device's "Send test" button: at most one press per endpoint, and per
# member per push-service host, in this interval. Endpoints are chosen by the
# caller, so these alone do not bound the hub's outbound requests; the
# per-tier budget below (TestPushLimiter) does.
TEST_PUSH_INTERVAL_S = 10.0

# Subscription quotas, per tier. A self-declared guest gets a fresh member id
# with every new cookie, so a per-member cap alone does not bound guests; the
# guest pool is therefore small and SEPARATE, and nothing a guest does can
# consume the room kept for the owner, local users and listed members.
TIER_GUEST = "guest"
TIER_TRUSTED = "trusted"
TIER_QUOTAS = {               # tier -> (per member, whole tier)
    TIER_GUEST: (8, 200),
    TIER_TRUSTED: (64, 4800),
}
# Rows written before the tier column existed. Their owner is unknown, so
# they are never aged out (that could delete the owner's subscriptions) and
# count against the trusted pool. The page re-posts its subscription on load,
# which re-tiers the row from the server-side identity.
TIER_LEGACY = "legacy"
# Which stored tiers draw on each pool.
TIER_POOL_MEMBERS = {
    TIER_GUEST: (TIER_GUEST,),
    TIER_TRUSTED: (TIER_TRUSTED, TIER_LEGACY),
}

# Delivery bounds. Sends run in parallel and each tick stops taking new
# messages once this many pushes are queued; the rest wait for the next tick.
MAX_SENDS_PER_TICK = 256
SEND_WORKERS = 16
TRANSIENT_BACKOFF_S = 60
MAX_CONSECUTIVE_REJECTS = 3   # 4xx in a row before a subscription is dropped
BANG_RETRY_ATTEMPTS = 3
MAX_RETRY_QUEUE = 500
LEASE_RECHECK_S = 2.0         # how often a long send re-asks for the lease
SWEEP_INTERVAL_S = 60.0       # orphan / idle-guest sweep cadence
GUEST_IDLE_S = 30 * 86400     # guest rows with no update or delivery this long go
MARKER_INTERVAL_S = 10.0      # how often the dispatcher re-advertises itself
# An advertisement older than this no longer counts. Above the worst-case tick
# (256 sends / 16 workers x 10 s timeout = 160 s), and the marker is also
# refreshed during long send phases; a dead hub drops out sooner anyway,
# when its lease expires.
MARKER_FRESH_S = 300.0
STATUS_SKIPPED = -1           # a push not attempted because the lease is in doubt
MAX_RECORD_BACKLOG = 5000     # outcome writes kept while the DB refuses them

# What a lease check may answer. EXPIRED is our own row past its expiry
# (renewals failing: a locked DB, a suspend, a clock step) and only pauses
# delivery; LOST means another hub holds the lease and ends it.
LEASE_HELD, LEASE_EXPIRED, LEASE_LOST = "held", "expired", "lost"

# Push services the browsers we target actually use. A subscription endpoint is
# a URL the CLIENT supplies and the SERVER later POSTs to, so without this list
# any named visitor could aim the hub's outbound requests at an internal
# address. Exact hosts, then suffixes (each suffix begins with a dot so
# "evilpush.apple.com" cannot match ".push.apple.com").
PUSH_HOSTS_EXACT = frozenset((
    "fcm.googleapis.com",                   # Chrome, Edge on Android, Samsung
    "updates.push.services.mozilla.com",    # Firefox
    "web.push.apple.com",                   # Safari, iOS/iPadOS home-screen apps
))
PUSH_HOST_SUFFIXES = (
    ".push.apple.com",
    ".notify.windows.com",                  # Edge/Windows WNS
)


# ───────── base64url ─────────

def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = (text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]*={0,2}", text):
        raise ValueError("not base64url")
    text = text.rstrip("=")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ───────── SSRF guard ─────────

def endpoint_allowed(endpoint: str) -> bool:
    """Whether a subscription endpoint is one we are willing to POST to.

    https only, default port only, no userinfo, and a host on the known
    push-service list. Checked at subscribe time AND again before every send,
    so a row written by an older build cannot bypass it.
    """
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_LEN:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 for ch in endpoint):
        return False
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False
    if port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host in PUSH_HOSTS_EXACT:
        return True
    return any(host.endswith(suffix) and len(host) > len(suffix)
               for suffix in PUSH_HOST_SUFFIXES)


def validate_subscription(sub: Any) -> Tuple[str, str, str]:
    """(endpoint, p256dh, auth) from a PushSubscription.toJSON() object.

    Raises ValueError with a sentence fit for the API caller.
    """
    if not isinstance(sub, dict):
        raise ValueError("subscription must be an object")
    endpoint = sub.get("endpoint")
    if not endpoint_allowed(endpoint):
        raise ValueError("subscription endpoint is not a recognised push service")
    keys = sub.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys missing")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        raise ValueError("subscription keys missing")
    try:
        ua_public = b64url_decode(p256dh)
        auth_secret = b64url_decode(auth)
    except (ValueError, TypeError):
        raise ValueError("subscription keys are not base64url") from None
    if len(ua_public) != 65 or ua_public[0] != 0x04:
        raise ValueError("p256dh must be an uncompressed P-256 point")
    if len(auth_secret) != 16:
        raise ValueError("auth secret must be 16 bytes")
    if HAVE_CRYPTO:
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
        except ValueError:
            raise ValueError("p256dh is not a point on P-256") from None
    return endpoint, b64url_encode(ua_public), b64url_encode(auth_secret)


# ───────── RFC 8291 encryption ─────────

def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt_aes128gcm(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *,
                      salt: Optional[bytes] = None,
                      as_private: Optional["ec.EllipticCurvePrivateKey"] = None,
                      record_size: int = RECORD_SIZE) -> bytes:
    """Encrypt one push message body (RFC 8291 section 3.4, RFC 8188).

    `salt` and `as_private` are parameters only so the RFC test vector can be
    reproduced; production callers leave both None and get fresh random ones
    per message, which the RFC requires.
    """
    if not HAVE_CRYPTO:
        raise RuntimeError("web push needs the cryptography package")
    if len(auth_secret) != 16:
        raise ValueError("auth secret must be 16 bytes")
    if salt is None:
        salt = os.urandom(16)
    if as_private is None:
        as_private = ec.generate_private_key(ec.SECP256R1())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_public = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)

    prk_key = _hmac_sha256(auth_secret, ecdh_secret)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hmac_sha256(prk_key, key_info + b"\x01")
    prk = _hmac_sha256(salt, ikm)
    cek = _hmac_sha256(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac_sha256(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    # A single record: the plaintext plus the 0x02 "last record" delimiter,
    # and the whole body (header + record) within the push services' limit.
    if len(plaintext) > MAX_PLAINTEXT or len(plaintext) + 1 + 16 > record_size:
        raise ValueError("push payload too large for one record")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", record_size, len(as_public)) + as_public
    return header + ciphertext


# ───────── RFC 8292 VAPID ─────────

class VapidKeys:
    """The hub's long-lived VAPID signing key.

    Created on first use in the hub state directory (next to nth.db) with mode
    0600. Browsers bind each subscription to this public key, so replacing it
    silently would orphan every subscription; a key file that exists but
    cannot be read is therefore an error, never a reason to make a new one.
    """

    def __init__(self, private_key: "ec.EllipticCurvePrivateKey"):
        self._private = private_key
        self.public_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_b64 = b64url_encode(self.public_bytes)
        self._jwt_cache: Dict[Tuple[str, str], Tuple[str, int]] = {}
        self._lock = threading.Lock()

    @classmethod
    def load_or_create(cls, state_dir: Path) -> "VapidKeys":
        if not HAVE_CRYPTO:
            raise RuntimeError("web push needs the cryptography package")
        path = Path(state_dir) / VAPID_KEY_FILENAME
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, pem)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                # link() fails if another process won the race, which is what
                # we want: both then load the single winner's key.
                os.link(str(tmp), str(path))
            except FileExistsError:
                pass
            except OSError:
                if not path.exists():
                    os.replace(str(tmp), str(path))
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(str(path), 0o600)
        except OSError:
            pass
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise RuntimeError(f"{path.name} is not a P-256 private key")
        return cls(key)

    def sign_jwt(self, claims: Dict[str, Any]) -> str:
        header = {"typ": "JWT", "alg": "ES256"}
        signing_input = (b64url_encode(json.dumps(header, separators=(",", ":")).encode())
                         + "." +
                         b64url_encode(json.dumps(claims, separators=(",", ":")).encode()))
        der = self._private.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        # JWS ES256 wants the raw 64-byte r||s, not DER.
        return signing_input + "." + b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def authorization(self, endpoint: str, contact: str,
                      now: Optional[float] = None) -> str:
        """The Authorization header value for a push to `endpoint`."""
        now = time.time() if now is None else now
        audience = vapid_audience(endpoint)
        with self._lock:
            cached = self._jwt_cache.get((audience, contact))
            if cached is None or cached[1] - now < JWT_REFRESH_MARGIN_S:
                exp = int(now) + JWT_LIFETIME_S
                token = self.sign_jwt({"aud": audience, "exp": exp, "sub": contact})
                cached = (token, exp)
                self._jwt_cache[(audience, contact)] = cached
        return f"vapid t={cached[0]}, k={self.public_b64}"


def vapid_audience(endpoint: str) -> str:
    """The JWT `aud`: the push service origin in canonical form.

    Built from the hostname alone (lower case, no trailing dot, no port) so an
    endpoint spelled "https://FCM.googleapis.com.:443/..." signs for the origin
    the push service actually checks. endpoint_allowed() admits only https on
    the default port, so the scheme and port are fixed.
    """
    host = (urlparse(endpoint).hostname or "").lower().rstrip(".")
    return f"https://{host}"


def verify_jwt(token: str, public_bytes: bytes) -> Dict[str, Any]:
    """Verify an ES256 JWT against an uncompressed P-256 public key.

    Used by the tests (and handy for debugging); raises on a bad signature.
    """
    header_b64, claims_b64, sig_b64 = token.split(".")
    sig = b64url_decode(sig_b64)
    if len(sig) != 64:
        raise ValueError("ES256 signature must be 64 bytes")
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"),
                               int.from_bytes(sig[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_bytes)
    pub.verify(der, f"{header_b64}.{claims_b64}".encode("ascii"),
               ec.ECDSA(hashes.SHA256()))
    return json.loads(b64url_decode(claims_b64))


_VAPID_CACHE: Dict[str, VapidKeys] = {}
_VAPID_LOCK = threading.Lock()


def vapid_for(state_dir: Path) -> VapidKeys:
    """Process-wide VapidKeys for a state directory, created on first use."""
    key = str(Path(state_dir).resolve())
    with _VAPID_LOCK:
        keys = _VAPID_CACHE.get(key)
        if keys is None:
            keys = VapidKeys.load_or_create(Path(state_dir))
            _VAPID_CACHE[key] = keys
        return keys


def push_contact() -> str:
    contact = (os.environ.get("NTH_PUSH_CONTACT") or "").strip()
    if contact.startswith(("mailto:", "https://")):
        return contact
    return DEFAULT_CONTACT


# ───────── Who gets told what (pure) ─────────

@dataclass(frozen=True)
class SubState:
    """Per-subscription delivery state the frequency logic carries forward."""
    last_sent_at: float = 0.0
    pending_count: int = 0
    pending_sender: str = ""


@dataclass(frozen=True)
class Decision:
    send: bool
    kind: str = ""               # "message" | "bang" | "digest" | ""
    state: SubState = SubState()
    count: int = 0               # digest only: messages summarised
    sender: str = ""             # digest only: the latest sender


# Hub lifecycle notices ("[claimed #12 ...", "[joined] ..."). Mirrors
# isSystemContent in web/js/10-markdown.js, which keeps them out of the page's
# own chimes and popups for the same reason: nobody wants a phone buzz per
# task claim.
_SYSTEM_WORDS = frozenset((
    "claimed", "done", "cancelled", "released", "retracted", "joined", "left",
    "ended", "locked", "unlocked", "status", "pinned", "renamed", "culled",
    "objective", "superseded"))
_SYSTEM_RE = re.compile(r"^\[([a-z]+)(?:\s|\](?:\s|$))")


def is_system_content(content: str) -> bool:
    if re.match(r"^\[channel created\](?:\s|$)", content or ""):
        return True
    m = _SYSTEM_RE.match(content or "")
    return bool(m) and m.group(1) in _SYSTEM_WORDS


def _sigil_hit(sigil: str, content: str, member_id: str, name: str) -> bool:
    """`@all` / `@name` / `@member_id` (or `!`) in the text, as the hub parses it.

    The hub resolves sigils against the channel ROSTER when the message is
    posted, so a viewer who has never posted (and so is not on that roster) is
    absent from the stored arrays. This text scan covers that case with the
    same patterns nth_web._parse_sigils_against_roster uses.
    """
    targets = ["all"]
    for value in (name, member_id):
        if value and value.lower() != "all":
            targets.append(re.escape(value))
    pattern = re.escape(sigil) + "(?:" + "|".join(targets) + r")(?:\b|$)"
    return re.search(pattern, content or "", re.IGNORECASE) is not None


def is_targeted(msg: Dict[str, Any], member_id: str, name: str) -> Tuple[bool, bool]:
    """(mentioned, banged) for this viewer.

    A DM addressed to the viewer counts as a mention: it is the most direct
    form of being addressed there is.
    """
    content = msg.get("content") or ""
    mentions = msg.get("mentions") or []
    bangs = msg.get("bangs") or []
    recipients = parse_recipients(msg.get("recipients"))
    mentioned = (member_id in mentions or member_id in recipients
                 or _sigil_hit("@", content, member_id, name))
    banged = member_id in bangs or _sigil_hit("!", content, member_id, name)
    return mentioned, banged


def decide(mode: str, msg: Dict[str, Any], member_id: str, name: str,
           state: SubState, now: float) -> Decision:
    """Whether one new message produces a push for one subscription.

    Rules, in order:
      * `off` never notifies.
      * Your own message, a retracted one, a system notice, or a DM you are not
        a party to never notifies.
      * A bang addressed to you (`!name`, `!all`) always notifies at once, in
        every mode but `off` -- bangs cross every filter, as they do for agents.
      * `all` notifies for every message.
      * `mentions` notifies for `@name`, `@member_id`, `@all`, or a DM to you.
      * `every5m` counts messages and sends one summary at most every five
        minutes; `flush_due` sends the summary once the window opens.
    """
    unchanged = Decision(False, state=state)
    if mode not in PUSH_MODES or mode == "off":
        return unchanged
    sender = msg.get("member_id") or ""
    if not member_id or sender == member_id:
        return unchanged
    if msg.get("retracted_at"):
        return unchanged
    if is_system_content(msg.get("content") or ""):
        return unchanged
    if not can_see(member_id, None, sender, msg.get("recipients"),
                   allow_all_seeing=False):
        return unchanged
    mentioned, banged = is_targeted(msg, member_id, name)
    if banged:
        return Decision(True, "bang", state)
    if mode == "all":
        return Decision(True, "message", replace(state, last_sent_at=now))
    if mode == "mentions":
        if mentioned:
            return Decision(True, "message", replace(state, last_sent_at=now))
        return unchanged
    # every5m
    count = state.pending_count + 1
    sender_name = msg.get("member_name") or sender
    if now - state.last_sent_at >= DIGEST_INTERVAL_S:
        return Decision(True, "digest", SubState(now, 0, ""), count, sender_name)
    return Decision(False, state=SubState(state.last_sent_at, count, sender_name))


def flush_due(mode: str, state: SubState, now: float) -> Decision:
    """Send a held every5m summary once its five-minute window has passed."""
    if (mode == "every5m" and state.pending_count > 0
            and now - state.last_sent_at >= DIGEST_INTERVAL_S):
        return Decision(True, "digest", SubState(now, 0, ""),
                        state.pending_count, state.pending_sender)
    return Decision(False, state=state)


def build_payload(channel: str, decision: Decision,
                  msg: Optional[Dict[str, Any]] = None,
                  show_text: bool = False) -> Dict[str, Any]:
    """The JSON the service worker turns into a notification.

    `tag` is per channel so a burst collapses into one notification on the
    phone rather than stacking twenty.

    The title names the channel (or DM) and the sender either way. The message
    text goes in the body only when the device opted in with `show_text`;
    otherwise the body is HIDDEN_BODY. A digest names only the latest sender,
    which the titles already disclose, so it is the same in both cases.
    """
    base = {"channel": channel, "tag": f"nth-{channel}", "url": f"/?channel={channel}"}
    if decision.kind == "digest":
        n = decision.count
        noun = "message" if n == 1 else "messages"
        return {**base, "title": f"#{channel} — {n} new {noun}",
                "body": f"Latest from {decision.sender}" if decision.sender else ""}
    msg = msg or {}
    sender = msg.get("member_name") or msg.get("member_id") or "someone"
    where = "DM" if parse_recipients(msg.get("recipients")) else f"#{channel}"
    title = f"{where} — {sender}"
    if decision.kind == "bang":
        title = "Urgent: " + title
    if show_text:
        body = _clip(" ".join(str(msg.get("content") or "").split()), MAX_BODY_CHARS)
    else:
        body = HIDDEN_BODY
    return {**base, "title": _clip(title, MAX_TITLE_CHARS), "body": body}


def test_payload(channel: str) -> Dict[str, Any]:
    """The notification a device's "Send test" button asks for.

    Its own tag, so it never replaces a real notification for the channel
    that is still waiting to be read.
    """
    return {"channel": channel, "tag": "nth-test", "url": f"/?channel={channel}",
            "title": _clip(f"Test notification from #{channel}", MAX_TITLE_CHARS),
            "body": "If you can read this, notifications reach this device."}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def encode_payload(payload: Dict[str, Any]) -> bytes:
    """Serialise a payload so it always fits one push record.

    UTF-8 rather than \\u escapes (an emoji costs 4 octets, not 12), and the
    body is shortened until the whole thing fits MAX_PLAINTEXT.
    """
    payload = dict(payload)
    while True:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(raw) <= MAX_PLAINTEXT:
            return raw
        body, title = str(payload.get("body") or ""), str(payload.get("title") or "")
        # Each step strictly shortens the field (len // 2 - 1 chars plus "…").
        if len(body) > 1:
            payload["body"] = body[:len(body) // 2 - 1] + "…"
        elif len(title) > 1:
            payload["title"] = title[:len(title) // 2 - 1] + "…"
        else:
            raise ValueError("push payload cannot be made to fit")


# ───────── Subscription store (SQLite, next to the hub DB) ─────────

def ensure_push_table(db: sqlite3.Connection) -> None:
    """Create the subscription table if missing. Additive only, so it is safe
    against a database any older or newer build has touched."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS push_subscriptions ("
        " channel TEXT NOT NULL,"
        " endpoint TEXT NOT NULL,"
        " member_id TEXT NOT NULL,"
        " member_name TEXT NOT NULL DEFAULT '',"
        " p256dh TEXT NOT NULL,"
        " auth TEXT NOT NULL,"
        " mode TEXT NOT NULL,"
        " created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL,"
        " last_sent_at REAL NOT NULL DEFAULT 0,"
        " pending_count INTEGER NOT NULL DEFAULT 0,"
        " pending_sender TEXT NOT NULL DEFAULT '',"
        " tier TEXT NOT NULL DEFAULT 'legacy',"
        " fail_count INTEGER NOT NULL DEFAULT 0,"
        " last_ok_at REAL NOT NULL DEFAULT 0,"
        " show_text INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (channel, endpoint))")
    # Columns added after the table first shipped. Every insert names its
    # tier, so the default only ever lands on rows that predate the column:
    # those become 'legacy' (see TIER_LEGACY), never 'guest'. Rows from before
    # show_text existed get 0: message text stays hidden until the device's
    # owner turns it on.
    have = {r[1] for r in db.execute("PRAGMA table_info(push_subscriptions)").fetchall()}
    for column, ddl in (("tier", "TEXT NOT NULL DEFAULT 'legacy'"),
                        ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
                        ("last_ok_at", "REAL NOT NULL DEFAULT 0"),
                        ("show_text", "INTEGER NOT NULL DEFAULT 0")):
        if column not in have:
            try:
                db.execute(f"ALTER TABLE push_subscriptions ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                # Two processes starting together can both see the column
                # missing; the loser's ALTER finds it already there.
                if "duplicate column name" not in str(exc).lower():
                    raise
    db.execute("CREATE INDEX IF NOT EXISTS idx_push_member "
               "ON push_subscriptions(member_id, channel)")
    # The running dispatcher advertises itself here, naming the lease holder
    # it belongs to, so a dashboard can tell "a hub is sending pushes" apart
    # from "a hub holds the lease" (an older build, or one without crypto).
    db.execute("CREATE TABLE IF NOT EXISTS push_dispatcher ("
               " id INTEGER PRIMARY KEY CHECK (id = 1),"
               " holder TEXT NOT NULL,"
               " version TEXT NOT NULL DEFAULT '',"
               " heartbeat_at REAL NOT NULL)")


def dispatcher_marker(db: sqlite3.Connection) -> Optional[Tuple[str, str, float]]:
    """(holder, version, heartbeat_at) of the advertised dispatcher, if any."""
    try:
        row = db.execute("SELECT holder, version, heartbeat_at FROM push_dispatcher "
                         "WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    return (row[0], row[1], float(row[2])) if row else None


class SubscriptionLimit(Exception):
    pass


class SubscriptionConflict(Exception):
    """The endpoint is already subscribed to this channel by another identity."""


def upsert_subscription(db: sqlite3.Connection, *, channel: str, endpoint: str,
                        p256dh: str, auth: str, member_id: str, member_name: str,
                        mode: str, tier: str = TIER_GUEST,
                        show_text: Optional[bool] = None,
                        now: Optional[float] = None) -> None:
    """Store one device's subscription to one channel.

    Keyed by (channel, endpoint): one browser has one endpoint, so changing
    mode from it replaces its row. A row belongs to the identity that created
    it; another identity presenting the same endpoint is refused
    (SubscriptionConflict) and the page answers by making a fresh endpoint.
    Changing the mode clears any every5m count held under the old one.

    `show_text` None keeps a row's current choice (a new row hides text): the
    page renews its subscription quietly on every visit, and that must never
    undo what the person ticked.
    """
    if mode not in SUBSCRIBE_MODES:
        raise ValueError("unknown mode (to turn notifications off, unsubscribe)")
    if tier not in TIER_QUOTAS:
        raise ValueError("unknown tier")
    now = time.time() if now is None else now
    ensure_push_table(db)
    # The quota check and the insert are one write transaction; two requests
    # racing for the last slot would otherwise both see room and both insert.
    owns_txn = not db.in_transaction
    if owns_txn:
        db.execute("BEGIN IMMEDIATE")
    try:
        _upsert_locked(db, channel, endpoint, p256dh, auth, member_id, member_name,
                       mode, tier, show_text, now)
    except BaseException:
        if owns_txn:
            db.rollback()
        raise
    if owns_txn:
        db.commit()


def _upsert_locked(db, channel, endpoint, p256dh, auth, member_id, member_name,
                   mode, tier, show_text, now) -> None:
    existing = db.execute(
        "SELECT member_id FROM push_subscriptions WHERE channel = ? AND endpoint = ?",
        (channel, endpoint)).fetchone()
    if existing is not None and existing[0] != member_id:
        raise SubscriptionConflict("this device is subscribed under another identity")
    if existing is None:
        per_member, per_tier = TIER_QUOTAS[tier]
        mine = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE member_id = ?",
                          (member_id,)).fetchone()[0]
        members = TIER_POOL_MEMBERS[tier]
        marks = ",".join("?" for _ in members)
        pool = db.execute(f"SELECT COUNT(*) FROM push_subscriptions WHERE tier IN ({marks})",
                          members).fetchone()[0]
        if mine >= per_member or pool >= per_tier:
            raise SubscriptionLimit("too many push subscriptions")
    shown = None if show_text is None else int(bool(show_text))
    db.execute(
        "INSERT INTO push_subscriptions (channel, endpoint, member_id, member_name,"
        " p256dh, auth, mode, created_at, updated_at, tier, show_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE(?, 0))"
        " ON CONFLICT(channel, endpoint) DO UPDATE SET"
        " member_name = excluded.member_name, p256dh = excluded.p256dh,"
        " auth = excluded.auth, updated_at = excluded.updated_at, fail_count = 0,"
        " show_text = COALESCE(?, push_subscriptions.show_text),"
        # The tier comes from the server-side identity of whoever is posting
        # now, so it is rewritten on every renewal (re-tiering legacy rows).
        " tier = excluded.tier,"
        " pending_count = CASE WHEN push_subscriptions.mode = excluded.mode"
        "   THEN push_subscriptions.pending_count ELSE 0 END,"
        " pending_sender = CASE WHEN push_subscriptions.mode = excluded.mode"
        "   THEN push_subscriptions.pending_sender ELSE '' END,"
        " mode = excluded.mode"
        " WHERE push_subscriptions.member_id = excluded.member_id",
        (channel, endpoint, member_id, member_name, p256dh, auth, mode, now, now, tier,
         shown, shown))


def move_endpoint(db: sqlite3.Connection, *, member_id: str, old_endpoint: str,
                  new_endpoint: str, p256dh: str, auth: str,
                  now: Optional[float] = None) -> List[str]:
    """Re-point this identity's subscriptions from one endpoint to another.

    Used when the page has to replace its browser subscription. One write
    transaction, row count unchanged, so a member at its quota keeps every
    channel. A channel already subscribed on the new endpoint keeps that row
    and only the old one is removed. Returns the channels moved.
    """
    now = time.time() if now is None else now
    ensure_push_table(db)
    owns_txn = not db.in_transaction
    if owns_txn:
        db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute("SELECT channel FROM push_subscriptions WHERE member_id = ? "
                          "AND endpoint = ?", (member_id, old_endpoint)).fetchall()
        moved = []
        for (channel,) in rows:
            taken = db.execute("SELECT 1 FROM push_subscriptions WHERE channel = ? AND "
                               "endpoint = ?", (channel, new_endpoint)).fetchone()
            if taken:
                db.execute("DELETE FROM push_subscriptions WHERE channel = ? AND endpoint = ?",
                           (channel, old_endpoint))
                continue
            # last_ok_at described the old endpoint; nothing has been
            # delivered to the new one yet. The text choice carries over.
            db.execute("UPDATE push_subscriptions SET endpoint = ?, p256dh = ?, auth = ?, "
                       "updated_at = ?, fail_count = 0, last_ok_at = 0 "
                       "WHERE channel = ? AND endpoint = ?",
                       (new_endpoint, p256dh, auth, now, channel, old_endpoint))
            moved.append(channel)
    except BaseException:
        if owns_txn:
            db.rollback()
        raise
    if owns_txn:
        db.commit()
    return moved


def delete_subscription(db: sqlite3.Connection, *, member_id: str, endpoint: str,
                        channel: Optional[str] = None) -> int:
    ensure_push_table(db)
    if channel:
        cur = db.execute("DELETE FROM push_subscriptions WHERE member_id = ? AND "
                         "endpoint = ? AND channel = ?", (member_id, endpoint, channel))
    else:
        cur = db.execute("DELETE FROM push_subscriptions WHERE member_id = ? AND "
                         "endpoint = ?", (member_id, endpoint))
    return cur.rowcount


def set_show_text(db: sqlite3.Connection, *, member_id: str, endpoint: str,
                  channel: str, show_text: bool) -> int:
    """Change whether one of this identity's subscriptions shows message text.
    Returns the rows changed: 0 when the row is missing or someone else's."""
    ensure_push_table(db)
    return db.execute("UPDATE push_subscriptions SET show_text = ? WHERE member_id = ? "
                      "AND endpoint = ? AND channel = ?",
                      (int(bool(show_text)), member_id, endpoint, channel)).rowcount


def own_subscription(db: sqlite3.Connection, *, member_id: str, endpoint: str,
                     channel: str) -> Optional[Dict[str, Any]]:
    """This identity's row for one endpoint and channel, or None. The only way
    the HTTP surface finds a row to act on, so nobody acts on another's."""
    ensure_push_table(db)
    row = db.execute("SELECT endpoint, p256dh, auth, mode, show_text, last_ok_at "
                     "FROM push_subscriptions WHERE member_id = ? AND endpoint = ? "
                     "AND channel = ?", (member_id, endpoint, channel)).fetchone()
    if row is None:
        return None
    return {"endpoint": row[0], "p256dh": row[1], "auth": row[2], "mode": row[3],
            "show_text": bool(row[4]), "last_ok_at": float(row[5] or 0)}


def record_test_outcome(db: sqlite3.Connection, *, channel: str, endpoint: str,
                        status: int, now: Optional[float] = None) -> None:
    """Apply a test push's result the way the dispatcher applies a delivery's:
    success marks the row delivered, 404/410 forgets an endpoint the push
    service says is gone. Any other refusal leaves the rejection count alone;
    the dispatcher keeps that count from real deliveries only."""
    now = time.time() if now is None else now
    ensure_push_table(db)
    if 200 <= status < 300:
        db.execute("UPDATE push_subscriptions SET fail_count = 0, last_ok_at = ? "
                   "WHERE channel = ? AND endpoint = ?", (now, channel, endpoint))
    elif status in (404, 410):
        db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


# Per-process test budget per quota tier: (burst, tests per minute). A guest can
# mint identities and endpoints freely, so the whole guest tier shares a small
# budget of its own; exhausting it never touches the trusted tier's.
TEST_PUSH_TIER_BUDGET = {
    TIER_GUEST: (5, 5.0),
    TIER_TRUSTED: (30, 30.0),
}


class TestPushLimiter:
    """Bounds the test pushes this process sends.

    Three limits, all of which must allow a press before any is charged:
      * one press per endpoint per TEST_PUSH_INTERVAL_S;
      * one press per (member, push-service host) per interval, so one
        identity cycling fresh endpoints on the same service gains nothing;
      * a token bucket per quota tier (TEST_PUSH_TIER_BUDGET), which is what
        caps the hub's outbound requests no matter how many identities and
        endpoints a caller creates.

    Callers must check ownership first, so nobody can spend another device's
    allowance. In memory on purpose: the button is a convenience, a restart
    resetting it costs nothing, and keeping it out of the DB keeps the test
    path from taking the write lock just to say no.
    """

    def __init__(self, interval_s: float = TEST_PUSH_INTERVAL_S,
                 budgets: Optional[Dict[str, Tuple[int, float]]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.interval_s = interval_s
        self.budgets = dict(TEST_PUSH_TIER_BUDGET if budgets is None else budgets)
        # The refill rate divides by per_minute.
        if any(per_minute <= 0 for _, per_minute in self.budgets.values()):
            raise ValueError('every tier needs a positive per-minute budget')
        self._clock = clock
        self._last: Dict[Tuple[str, ...], float] = {}
        # tier -> (tokens, refilled_at)
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, *, member_id: str, endpoint: str, tier: str) -> float:
        """0 when the caller may send now (and every limit is charged), else
        the seconds until the tightest limit allows it."""
        # An unknown tier shares the guest bucket, the smallest budget there is.
        if tier not in self.budgets:
            tier = TIER_GUEST
        burst, per_minute = self.budgets[tier]
        keys = (("endpoint", endpoint),
                ("member-host", member_id, _host_of(endpoint).lower().rstrip(".")))
        with self._lock:
            now = self._clock()
            # Forget expired entries so the map stays as small as the set of
            # presses in the last interval.
            for k in [k for k, t in self._last.items() if now - t >= self.interval_s]:
                del self._last[k]
            wait = max((self.interval_s - (now - self._last[k]) for k in keys
                        if k in self._last), default=0.0)
            tokens, at = self._buckets.get(tier, (float(burst), now))
            tokens = min(float(burst), tokens + (now - at) * per_minute / 60.0)
            self._buckets[tier] = (tokens, now)
            if tokens < 1.0:
                wait = max(wait, (1.0 - tokens) * 60.0 / per_minute)
            if wait > 0:
                return wait
            self._buckets[tier] = (tokens - 1.0, now)
            for k in keys:
                self._last[k] = now
            return 0.0


TEST_LIMITER = TestPushLimiter()


def delete_channel_subscriptions(db: sqlite3.Connection, channel: str) -> int:
    """Forget every subscription to a channel that is being deleted."""
    ensure_push_table(db)
    return db.execute("DELETE FROM push_subscriptions WHERE channel = ?",
                      (channel,)).rowcount


def subscriptions_of(db: sqlite3.Connection, member_id: str) -> List[Dict[str, str]]:
    """Every subscription this identity holds, across channels (bounded by the
    per-member quota). The page uses it to move them to a new endpoint."""
    ensure_push_table(db)
    rows = db.execute("SELECT channel, endpoint, mode FROM push_subscriptions WHERE "
                      "member_id = ? ORDER BY channel", (member_id,)).fetchall()
    return [{"channel": r[0], "endpoint": r[1], "mode": r[2]} for r in rows]


def subscriptions_for(db: sqlite3.Connection, member_id: str,
                      channel: str) -> List[Dict[str, Any]]:
    """This identity's subscriptions to one channel, with what the page shows
    for its own device: the text choice, and when the push service last
    accepted a notification for it on this channel (None = never). Acceptance
    is all the hub can know; whether the phone displayed it is not reported."""
    ensure_push_table(db)
    rows = db.execute("SELECT endpoint, mode, show_text, last_ok_at FROM push_subscriptions "
                      "WHERE member_id = ? AND channel = ?", (member_id, channel)).fetchall()
    return [{"endpoint": r[0], "mode": r[1], "show_text": bool(r[2]),
             "last_ok_at": float(r[3]) if r[3] else None} for r in rows]


# ───────── Delivery ─────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect from a push service would send our POST to a host the
    allowlist never approved; treat it as a failure instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def send_push(endpoint: str, p256dh: str, auth: str, payload: Dict[str, Any],
              vapid: VapidKeys, contact: str, *, urgency: str = "normal",
              ttl: int = PUSH_TTL_S, opener=None) -> int:
    """POST one encrypted push. Returns the HTTP status (0 = network error)."""
    if not endpoint_allowed(endpoint):
        return 0
    body = encrypt_aes128gcm(encode_payload(payload),
                             b64url_decode(p256dh), b64url_decode(auth))
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Authorization", vapid.authorization(endpoint, contact))
    req.add_header("Content-Encoding", "aes128gcm")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("TTL", str(int(ttl)))
    req.add_header("Urgency", urgency)
    try:
        with (opener or _OPENER).open(req, timeout=SEND_TIMEOUT_S) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _host_of(endpoint: str) -> str:
    """Endpoints are bearer capabilities; logs name only the push service."""
    try:
        return urlparse(endpoint).hostname or "?"
    except ValueError:
        return "?"


@dataclass
class _Outgoing:
    """One push about to be sent, with what is needed to undo or retry it."""
    sub: Dict[str, Any]
    decision: Decision
    msg: Optional[Dict[str, Any]]
    prior: SubState
    attempt: int = 0
    status: int = 0


def _transient(status: int) -> bool:
    return status == 0 or status == 429 or status >= 500




def _lease_state(answer: Any) -> str:
    """Normalise a lease check's answer; a bare bool means held / lost."""
    if answer is True:
        return LEASE_HELD
    if answer is False:
        return LEASE_LOST
    return answer if answer in (LEASE_HELD, LEASE_EXPIRED, LEASE_LOST) else LEASE_HELD


class PushDispatcher(threading.Thread):
    """Watches the hub DB for new messages and sends the pushes they earn.

    Same change detection as the dashboard's EventHub: a message-id high-water
    mark polled on an interval. Starts at the current maximum, so a restart
    never replays history to anyone's phone. Runs on its own daemon thread and
    catches everything: a broken push service or a locked DB costs a backoff,
    never the web server.

    `lease_check` is asked before every tick and during long sends. LEASE_LOST
    (another hub holds the lease) stops the dispatcher for good, since two
    senders would deliver every push twice. LEASE_EXPIRED (our own row, past
    its expiry because renewals are failing) only pauses: the hub keeps the
    lease through a locked DB or a suspend, and delivery resumes once it
    renews.

    `holder` and `version` are advertised in the push_dispatcher table so a
    dashboard can report whether pushes are actually being sent.

    Delivery guarantees: summaries and bangs survive a transient failure or a
    send skipped while the lease is in doubt (the count is handed back; the
    bang is retried a bounded number of times). Plain all/mentions messages
    are at most once: one that cannot be sent right then is dropped, and the
    next message on the channel reaches the phone as usual.
    """

    def __init__(self, db_path: Path, state_dir: Path, *, poll_s: float = 2.0,
                 sender: Callable[..., int] = send_push,
                 clock: Callable[[], float] = time.time,
                 lease_check: Optional[Callable[[], Any]] = None,
                 holder: str = "", version: str = ""):
        super().__init__(name="nth-push", daemon=True)
        self.db_path = Path(db_path)
        self.state_dir = Path(state_dir)
        self.poll_s = poll_s
        self._send = sender
        self._clock = clock
        self._lease_check = lease_check
        self.holder = holder
        self.version = version
        self._stop = threading.Event()
        self.high_water: Optional[int] = None
        # A message row only partly planned because the tick hit its cap:
        # (message id, keys already planned). The next tick finishes it.
        self._partial: Optional[Tuple[int, set]] = None
        # endpoint -> monotonic time before which nothing is sent to it
        self._endpoint_backoff: Dict[str, float] = {}
        # bangs whose send failed transiently, retried on later ticks
        self._retry: "deque[_Outgoing]" = deque()
        self._last_error = ""
        self._next_sweep = 0.0
        self._next_marker = 0.0
        self._lease_lock = threading.Lock()
        self._lease_checked_at = 0.0
        self._lease_ok = True
        self._planned_retries: List[_Outgoing] = []
        self._beat_lock = threading.Lock()
        # Outcome writes whose commit failed, reapplied at the next tick so a
        # handed-back summary count or a failure count is never lost.
        self._record_backlog: List[Tuple[str, Tuple[Any, ...]]] = []

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        delay = self.poll_s
        while not self._stop.wait(delay):
            try:
                self.tick()
                delay = self.poll_s
                self._last_error = ""
            except Exception as exc:  # never let the thread die
                msg = f"{type(exc).__name__}: {exc}"
                if msg != self._last_error:
                    sys.stderr.write(f"[nth_web push] {msg} (backing off)\n")
                    self._last_error = msg
                delay = min(max(delay * 2, self.poll_s), 60.0)

    # ── lease ──
    def _lease(self) -> str:
        if self._stop.is_set():
            return LEASE_LOST
        if self._lease_check is None:
            return LEASE_HELD
        try:
            state = _lease_state(self._lease_check())
        except Exception:
            state = LEASE_HELD      # a failed read is not evidence of a takeover
        if state == LEASE_LOST:
            sys.stderr.write("[nth_web push] another hub holds the agent-control "
                             "lease; phone notifications stop here\n")
            self._stop.set()
        return state

    def _still_ok(self) -> bool:
        """Re-ask for the lease during a long send, at most every LEASE_RECHECK_S."""
        with self._lease_lock:
            now = time.monotonic()
            if now - self._lease_checked_at >= LEASE_RECHECK_S:
                self._lease_ok = self._lease() == LEASE_HELD
                self._lease_checked_at = now
            ok = self._lease_ok
        # A long send phase keeps the advertisement fresh too.
        self._beat()
        return ok

    def _beat(self) -> None:
        """Advertise this dispatcher (holder + version), at most every
        MARKER_INTERVAL_S. Safe from worker threads; a failed write only
        delays the next advertisement."""
        if not self.holder:
            return
        with self._beat_lock:
            mono = time.monotonic()
            if mono < self._next_marker:
                return
            self._next_marker = mono + MARKER_INTERVAL_S
        try:
            db = sqlite3.connect(str(self.db_path), timeout=2)
            try:
                db.execute("INSERT OR REPLACE INTO push_dispatcher "
                           "(id, holder, version, heartbeat_at) VALUES (1, ?, ?, ?)",
                           (self.holder, self.version, time.time()))
                db.commit()
            finally:
                db.close()
        except sqlite3.Error:
            with self._beat_lock:
                self._next_marker = 0.0

    def _backed_off(self, endpoint: str) -> bool:
        return self._endpoint_backoff.get(endpoint, 0) > time.monotonic()

    # One poll. Public so tests can drive it without a thread.
    def tick(self) -> int:
        """Process new messages and due digests. Returns pushes attempted.

        Phases, so the hub DB is never write-locked while we wait on the
        network or think: housekeeping in its own short transactions, planning
        on reads only with the resulting state written in one short batch, the
        sends in parallel with no transaction open, then the outcomes (drop
        gone subscriptions, hand back undelivered summaries) in a final short
        batch. Agents post with a 5 s busy timeout, so no write here may hold
        the lock for longer than a statement or two.
        """
        if self._lease() != LEASE_HELD:
            return 0
        with self._lease_lock:
            self._lease_checked_at, self._lease_ok = time.monotonic(), True
        self._housekeeping()
        self._apply_record_backlog()
        outbox = self._plan()
        if not outbox:
            return 0
        workers = max(1, min(SEND_WORKERS, len(outbox)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nth-push-send") as pool:
            for item, status in zip(outbox, pool.map(self._deliver, outbox), strict=True):
                item.status = status
        self._record(outbox)
        return sum(1 for item in outbox if item.status != STATUS_SKIPPED)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=2000")
        return db

    def _housekeeping(self) -> None:
        mono = time.monotonic()
        if mono < self._next_sweep:
            self._beat()
            return
        db = self._connect()
        try:
            ensure_push_table(db)
            db.commit()
            if mono >= self._next_sweep:
                self._next_sweep = mono + SWEEP_INTERVAL_S
                # A channel deleted by any path (the MCP cleanup tool included)
                # takes its subscriptions with it; a guest row that has neither
                # been renewed nor delivered to in a month goes too, so a
                # guest cannot hold the guest pool with rows that never fire.
                db.execute("DELETE FROM push_subscriptions WHERE channel NOT IN "
                           "(SELECT code FROM channels)")
                db.commit()
                # Mode 'off' rows (left by an older build; subscribe no
                # longer creates them) can never deliver.
                db.execute("DELETE FROM push_subscriptions WHERE mode = 'off'")
                db.commit()
                # Only 'guest': a 'legacy' row's owner is unknown and may be
                # the hub owner, so it waits to be re-tiered instead.
                db.execute("DELETE FROM push_subscriptions WHERE tier = ? AND "
                           "MAX(updated_at, last_ok_at) < ?",
                           (TIER_GUEST, self._clock() - GUEST_IDLE_S))
                db.commit()
        finally:
            db.close()
        self._beat()

    def _plan(self) -> List[_Outgoing]:
        """Decide every push this poll earns and persist the new state.

        The high-water mark and the partial-row marker move only after the
        state write commits; if it fails, the retries taken off the queue go
        back on it and the same messages are planned again next tick.
        """
        db = self._connect()
        retries: List[_Outgoing] = []
        self._planned_retries: List[_Outgoing] = []
        try:
            top = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0] or 0)
            hw = top if self.high_water is None else self.high_water
            subs = [dict(r) for r in db.execute(
                "SELECT * FROM push_subscriptions WHERE mode != 'off'").fetchall()]
            if not subs:
                self.high_water, self._partial = top, None
                self._retry.clear()
                return []
            by_key = {(s["channel"], s["endpoint"]): s for s in subs}
            by_channel: Dict[str, List[Dict[str, Any]]] = {}
            for s in subs:
                by_channel.setdefault(s["channel"], []).append(s)
            now = self._clock()
            retries = self._due_retries(by_key)
            outbox: List[_Outgoing] = list(retries)
            dirty: Dict[Tuple[str, str], Dict[str, Any]] = {}
            new_hw, new_partial = hw, None
            if top > hw:
                chans = sorted(by_channel)
                marks = ",".join("?" for _ in chans)
                rows = db.execute(
                    "SELECT id, channel, member_id, member_name, content, mentions, bangs, "
                    "recipients, retracted_at FROM messages "
                    f"WHERE id > ? AND id <= ? AND channel IN ({marks}) "
                    "ORDER BY id ASC LIMIT 200",
                    (hw, top, *chans)).fetchall()
                capped = False
                for row in rows:
                    done = set()
                    if self._partial is not None and self._partial[0] == row["id"]:
                        done = set(self._partial[1])
                    try:
                        capped = self._plan_message(row, by_channel, now, outbox, dirty, done)
                    except Exception as exc:
                        # One bad row is logged and skipped; it must never pin
                        # the high-water mark and silence everything after it.
                        sys.stderr.write(f"[nth_web push] skipped message {row['id']}: "
                                         f"{type(exc).__name__}\n")
                        capped = False
                    if capped:
                        new_partial = (row["id"], done)
                        break
                    new_hw = row["id"]
                if not capped and len(rows) < 200:
                    new_hw = top
            for sub in subs:
                if len(outbox) >= MAX_SENDS_PER_TICK:
                    break                    # still pending; flushed next tick
                if self._backed_off(sub["endpoint"]):
                    continue                 # keep the summary until it can be delivered
                prior = _state_of(sub)
                d = flush_due(sub["mode"], prior, now)
                if d.send:
                    _set_state(sub, d.state, dirty)
                    outbox.append(_Outgoing(sub, d, None, prior))
            if dirty:
                # Conditional on the row being the one planned from: a mode
                # change (which resets the held count) or an unsubscribe and
                # re-subscribe (a new created_at) between the read and this
                # write must not be overwritten by stale absolute values.
                db.executemany(
                    "UPDATE push_subscriptions SET last_sent_at = ?, pending_count = ?, "
                    "pending_sender = ? WHERE channel = ? AND endpoint = ? "
                    "AND mode = ? AND created_at = ?",
                    [(s["last_sent_at"], s["pending_count"], s["pending_sender"],
                      s["channel"], s["endpoint"], s["mode"], s["created_at"])
                     for s in dirty.values()])
                db.commit()
        except BaseException:
            for item in reversed(retries):
                item.attempt -= 1
                self._retry.appendleft(item)
            raise
        finally:
            db.close()
        self.high_water, self._partial = new_hw, new_partial
        for item in self._planned_retries:
            self._queue_retry(item)
        return outbox

    def _plan_message(self, row, by_channel, now, outbox: List[_Outgoing],
                      dirty, done: set) -> bool:
        """Plan one message for every subscriber of its channel. Returns True
        when the tick's cap stopped it part-way; `done` then names the
        subscriptions already planned so the next tick resumes after them."""
        msg = _row_message(row)
        for sub in by_channel.get(row["channel"], ()):
            key = (sub["channel"], sub["endpoint"])
            if key in done:
                continue
            if len(outbox) >= MAX_SENDS_PER_TICK:
                return True
            prior = _state_of(sub)
            d = decide(sub["mode"], msg, sub["member_id"], sub["member_name"], prior, now)
            if d.send and self._backed_off(sub["endpoint"]):
                # A known-bad endpoint costs no request. A summary keeps
                # counting; a bang waits in the retry queue; a plain message
                # is dropped (the next one will reach the phone).
                if d.kind == "digest":
                    d = Decision(False, state=SubState(prior.last_sent_at, d.count, d.sender))
                elif d.kind == "bang":
                    # Queued only once the plan commits (see _plan).
                    self._planned_retries.append(_Outgoing(sub, d, msg, prior))
                    d = Decision(False, state=prior)
                else:
                    d = Decision(False, state=d.state)
            if d.state != prior:
                _set_state(sub, d.state, dirty)
            if d.send:
                outbox.append(_Outgoing(sub, d, msg, prior))
            done.add(key)
        return False

    def _queue_retry(self, item: _Outgoing) -> None:
        if item.attempt < BANG_RETRY_ATTEMPTS and len(self._retry) < MAX_RETRY_QUEUE:
            self._retry.append(item)

    def _due_retries(self, by_key) -> List[_Outgoing]:
        due, waiting = [], deque()
        while self._retry:
            item = self._retry.popleft()
            key = (item.sub["channel"], item.sub["endpoint"])
            current = by_key.get(key)
            if current is None or current["mode"] == "off":
                continue                     # unsubscribed meanwhile
            if self._backed_off(item.sub["endpoint"]) or len(due) >= MAX_SENDS_PER_TICK:
                waiting.append(item)
                continue
            item.sub = current
            item.attempt += 1
            due.append(item)
        self._retry = waiting
        return due

    def _deliver(self, item: _Outgoing) -> int:
        """Send one push; returns the HTTP status (0 = network error,
        STATUS_SKIPPED = not attempted). Runs on a worker thread."""
        if not self._still_ok():
            return STATUS_SKIPPED
        sub = item.sub
        endpoint = sub["endpoint"]
        payload = build_payload(sub["channel"], item.decision, item.msg,
                                show_text=bool(sub.get("show_text")))
        urgency = "high" if item.decision.kind == "bang" else "normal"
        try:
            return self._send(endpoint, sub["p256dh"], sub["auth"], payload,
                              vapid_for(self.state_dir), push_contact(),
                              urgency=urgency)
        except Exception as exc:
            # The type only: an endpoint is a bearer URL and must stay out of logs.
            sys.stderr.write(f"[nth_web push] send to {_host_of(endpoint)} failed: "
                             f"{type(exc).__name__}\n")
            return 0

    def _record(self, outbox: List[_Outgoing]) -> None:
        """Apply send outcomes: prune dead rows, restore undelivered summaries.

        A push skipped because the lease was in doubt (STATUS_SKIPPED), or
        that failed transiently, keeps a summary's count and re-queues a bang.
        A plain all/mentions message in that position is dropped: plain
        messages are delivered at most once by design, and the next message
        on the channel reaches the phone as usual.
        """
        now = self._clock()
        ops: List[Tuple[str, Tuple[Any, ...]]] = []
        for item in outbox:
            sub, status = item.sub, item.status
            endpoint, channel = sub["endpoint"], sub["channel"]
            if 200 <= status < 300:
                self._endpoint_backoff.pop(endpoint, None)
                ops.append(("UPDATE push_subscriptions SET fail_count = 0, last_ok_at = ? "
                            "WHERE channel = ? AND endpoint = ?", (now, channel, endpoint)))
            elif status in (404, 410):
                # The browser dropped this subscription; it never comes back.
                ops.append(("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)))
                self._endpoint_backoff.pop(endpoint, None)
            elif status == STATUS_SKIPPED or _transient(status):
                if status != STATUS_SKIPPED:
                    self._endpoint_backoff[endpoint] = time.monotonic() + TRANSIENT_BACKOFF_S
                if item.decision.kind == "digest":
                    # Hand the count back so the summary goes out later --
                    # unless the mode changed meanwhile, which reset it.
                    ops.append((
                        "UPDATE push_subscriptions SET pending_count = pending_count + ?, "
                        "last_sent_at = ?, pending_sender = CASE WHEN pending_sender = '' "
                        "THEN ? ELSE pending_sender END WHERE channel = ? AND endpoint = ? "
                        "AND mode = 'every5m' AND created_at = ?",
                        (item.decision.count, item.prior.last_sent_at, item.decision.sender,
                         channel, endpoint, sub["created_at"])))
                elif item.decision.kind == "bang":
                    if status == STATUS_SKIPPED:
                        item.attempt = max(0, item.attempt - 1)
                    self._queue_retry(item)
            else:
                # A 4xx the service will repeat (bad keys, bad auth, too
                # large). Three in a row and the subscription goes.
                ops.append(("UPDATE push_subscriptions SET fail_count = fail_count + 1 "
                            "WHERE channel = ? AND endpoint = ?", (channel, endpoint)))
                ops.append(("DELETE FROM push_subscriptions WHERE channel = ? AND "
                            "endpoint = ? AND fail_count >= ?",
                            (channel, endpoint, MAX_CONSECUTIVE_REJECTS)))
                if int(sub.get("fail_count") or 0) + 1 >= MAX_CONSECUTIVE_REJECTS:
                    sys.stderr.write(f"[nth_web push] dropping a subscription after "
                                     f"repeated refusals from {_host_of(endpoint)} "
                                     f"(HTTP {status})\n")
        self._record_backlog.extend(ops)
        self._apply_record_backlog()

    def _apply_record_backlog(self) -> None:
        """Write pending outcome ops in one transaction, retrying briefly. On
        failure they stay queued (bounded) for the next tick."""
        if not self._record_backlog:
            return
        ops = self._record_backlog
        for attempt in range(3):
            db = None
            try:
                db = self._connect()
                db.execute("BEGIN IMMEDIATE")
                for sql, params in ops:
                    db.execute(sql, params)
                db.commit()
                self._record_backlog = []
                return
            except sqlite3.Error as exc:
                if db is not None:
                    try:
                        db.rollback()
                    except sqlite3.Error:
                        pass
                if attempt == 2:
                    sys.stderr.write(f"[nth_web push] outcome write deferred: "
                                     f"{type(exc).__name__}\n")
                else:
                    time.sleep(0.2)
            finally:
                if db is not None:
                    db.close()
        # Keep the most recent outcomes if the DB stays unwritable for long.
        self._record_backlog = ops[-MAX_RECORD_BACKLOG:]


def _row_message(row: sqlite3.Row) -> Dict[str, Any]:
    def arr(raw):
        try:
            v = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            return []
        return [str(x) for x in v] if isinstance(v, list) else []
    return {
        "id": row["id"], "channel": row["channel"], "member_id": row["member_id"],
        "member_name": row["member_name"] or row["member_id"],
        "content": row["content"] or "", "mentions": arr(row["mentions"]),
        "bangs": arr(row["bangs"]), "recipients": row["recipients"],
        "retracted_at": row["retracted_at"],
    }


def _state_of(sub: Dict[str, Any]) -> SubState:
    return SubState(float(sub.get("last_sent_at") or 0.0),
                    int(sub.get("pending_count") or 0),
                    sub.get("pending_sender") or "")


def _set_state(sub: Dict[str, Any], state: SubState, dirty: Dict) -> None:
    """Record a new state in memory; _plan writes all of them in one batch."""
    sub["last_sent_at"] = state.last_sent_at
    sub["pending_count"] = state.pending_count
    sub["pending_sender"] = state.pending_sender
    dirty[(sub["channel"], sub["endpoint"])] = sub
