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
DIGEST_INTERVAL_S = 300          # every5m: at most one notification per window
DEFAULT_CONTACT = "mailto:admin@example.com"
VAPID_KEY_FILENAME = "push-vapid-key.pem"
JWT_LIFETIME_S = 12 * 3600       # RFC 8292 caps exp at 24h; 12h leaves margin
JWT_REFRESH_MARGIN_S = 3600
PUSH_TTL_S = 12 * 3600           # how long a push service holds an undelivered push
SEND_TIMEOUT_S = 10
RECORD_SIZE = 4096
MAX_ENDPOINT_LEN = 1024
MAX_BODY_CHARS = 240
MAX_SUBS_PER_MEMBER = 64
MAX_SUBS_TOTAL = 5000

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

    # A single record: the plaintext plus the 0x02 "last record" delimiter.
    # 16 bytes of tag and the delimiter must fit inside the record size.
    if len(plaintext) + 1 + 16 > record_size:
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
        parsed = urlparse(endpoint)
        audience = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            cached = self._jwt_cache.get((audience, contact))
            if cached is None or cached[1] - now < JWT_REFRESH_MARGIN_S:
                exp = int(now) + JWT_LIFETIME_S
                token = self.sign_jwt({"aud": audience, "exp": exp, "sub": contact})
                cached = (token, exp)
                self._jwt_cache[(audience, contact)] = cached
        return f"vapid t={cached[0]}, k={self.public_b64}"


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
                  msg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The JSON the service worker turns into a notification.

    `tag` is per channel so a burst collapses into one notification on the
    phone rather than stacking twenty.
    """
    base = {"channel": channel, "tag": f"nth-{channel}", "url": f"/?channel={channel}"}
    if decision.kind == "digest":
        n = decision.count
        noun = "message" if n == 1 else "messages"
        return {**base, "title": f"#{channel} — {n} new {noun}",
                "body": f"Latest from {decision.sender}" if decision.sender else ""}
    msg = msg or {}
    sender = msg.get("member_name") or msg.get("member_id") or "someone"
    body = " ".join(str(msg.get("content") or "").split())
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS - 1] + "…"
    where = "DM" if parse_recipients(msg.get("recipients")) else f"#{channel}"
    title = f"{where} — {sender}"
    if decision.kind == "bang":
        title = "Urgent: " + title
    return {**base, "title": title, "body": body}


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
        " PRIMARY KEY (channel, endpoint))")
    db.execute("CREATE INDEX IF NOT EXISTS idx_push_member "
               "ON push_subscriptions(member_id, channel)")


class SubscriptionLimit(Exception):
    pass


def upsert_subscription(db: sqlite3.Connection, *, channel: str, endpoint: str,
                        p256dh: str, auth: str, member_id: str, member_name: str,
                        mode: str, now: Optional[float] = None) -> None:
    """Store one device's subscription to one channel.

    Keyed by (channel, endpoint): one browser has one endpoint, so re-subscribing
    from it replaces its row -- including when that browser's identity changed,
    since the endpoint follows the device, not the cookie.
    """
    if mode not in PUSH_MODES:
        raise ValueError("unknown mode")
    now = time.time() if now is None else now
    ensure_push_table(db)
    existing = db.execute(
        "SELECT member_id FROM push_subscriptions WHERE channel = ? AND endpoint = ?",
        (channel, endpoint)).fetchone()
    if existing is None:
        mine = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE member_id = ?",
                          (member_id,)).fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
        if mine >= MAX_SUBS_PER_MEMBER or total >= MAX_SUBS_TOTAL:
            raise SubscriptionLimit("too many push subscriptions")
    db.execute(
        "INSERT INTO push_subscriptions (channel, endpoint, member_id, member_name,"
        " p256dh, auth, mode, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(channel, endpoint) DO UPDATE SET member_id = excluded.member_id,"
        " member_name = excluded.member_name, p256dh = excluded.p256dh,"
        " auth = excluded.auth, mode = excluded.mode, updated_at = excluded.updated_at",
        (channel, endpoint, member_id, member_name, p256dh, auth, mode, now, now))


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


def subscriptions_for(db: sqlite3.Connection, member_id: str,
                      channel: str) -> List[Dict[str, str]]:
    ensure_push_table(db)
    rows = db.execute("SELECT endpoint, mode FROM push_subscriptions WHERE "
                      "member_id = ? AND channel = ?", (member_id, channel)).fetchall()
    return [{"endpoint": r[0], "mode": r[1]} for r in rows]


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
    body = encrypt_aes128gcm(json.dumps(payload).encode("utf-8"),
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


class PushDispatcher(threading.Thread):
    """Watches the hub DB for new messages and sends the pushes they earn.

    Same change detection as the dashboard's EventHub: a message-id high-water
    mark polled on an interval. Starts at the current maximum, so a restart
    never replays history to anyone's phone. Runs on its own daemon thread and
    catches everything: a broken push service or a locked DB costs a backoff,
    never the web server.
    """

    def __init__(self, db_path: Path, state_dir: Path, *, poll_s: float = 2.0,
                 sender: Callable[..., int] = send_push,
                 clock: Callable[[], float] = time.time):
        super().__init__(name="nth-push", daemon=True)
        self.db_path = Path(db_path)
        self.state_dir = Path(state_dir)
        self.poll_s = poll_s
        self._send = sender
        self._clock = clock
        self._stop = threading.Event()
        self.high_water: Optional[int] = None
        # endpoint -> monotonic time before which we do not retry it
        self._endpoint_backoff: Dict[str, float] = {}
        self._last_error = ""

    def stop(self) -> None:
        self._stop.set()

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

    # One poll. Public so tests can drive it without a thread.
    def tick(self) -> int:
        """Process new messages and due digests. Returns pushes attempted.

        Three phases, so the hub DB is never locked while we wait on the
        network: decide and record state in one short transaction, send with
        no transaction open, then drop the subscriptions a push service
        reported gone in a second short transaction. A push service taking its
        full timeout must not make an agent's send hit "database is locked".
        """
        outbox = self._plan()
        gone = [endpoint for endpoint, status in
                ((item[0]["endpoint"], self._deliver(*item)) for item in outbox)
                if status in (404, 410)]
        if gone:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            try:
                # The browser dropped these subscriptions; they never come back.
                db.executemany("DELETE FROM push_subscriptions WHERE endpoint = ?",
                               [(e,) for e in gone])
                db.commit()
            finally:
                db.close()
        return len(outbox)

    def _plan(self) -> List[Tuple[Dict[str, Any], Decision, Optional[Dict[str, Any]]]]:
        """Decide every push this poll earns and persist the new state."""
        db = sqlite3.connect(str(self.db_path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=2000")
            ensure_push_table(db)
            top = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0] or 0)
            if self.high_water is None:
                self.high_water = top
            subs = [dict(r) for r in db.execute(
                "SELECT * FROM push_subscriptions WHERE mode != 'off'").fetchall()]
            if not subs:
                self.high_water = top
                db.commit()
                return []
            by_channel: Dict[str, List[Dict[str, Any]]] = {}
            for s in subs:
                by_channel.setdefault(s["channel"], []).append(s)
            outbox = []
            now = self._clock()
            if top > self.high_water:
                chans = sorted(by_channel)
                marks = ",".join("?" for _ in chans)
                limit = 200
                rows = db.execute(
                    "SELECT id, channel, member_id, member_name, content, mentions, bangs, "
                    "recipients, retracted_at FROM messages "
                    f"WHERE id > ? AND id <= ? AND channel IN ({marks}) "
                    "ORDER BY id ASC LIMIT ?",
                    (self.high_water, top, *chans, limit)).fetchall()
                for row in rows:
                    msg = _row_message(row)
                    for sub in by_channel.get(row["channel"], ()):
                        d = decide(sub["mode"], msg, sub["member_id"], sub["member_name"],
                                   _state_of(sub), now)
                        if d.state != _state_of(sub):
                            _save_state(db, sub, d.state)
                        if d.send:
                            outbox.append((sub, d, msg))
                self.high_water = rows[-1]["id"] if len(rows) == limit else top
            for sub in subs:
                d = flush_due(sub["mode"], _state_of(sub), now)
                if d.send:
                    _save_state(db, sub, d.state)
                    outbox.append((sub, d, None))
            db.commit()
            return outbox
        finally:
            db.close()

    def _deliver(self, sub: Dict[str, Any], decision: Decision,
                 msg: Optional[Dict[str, Any]]) -> int:
        """Send one push; returns the HTTP status (0 = not sent)."""
        endpoint = sub["endpoint"]
        if self._endpoint_backoff.get(endpoint, 0) > time.monotonic():
            return 0
        payload = build_payload(sub["channel"], decision, msg)
        urgency = "high" if decision.kind == "bang" else "normal"
        try:
            status = self._send(endpoint, sub["p256dh"], sub["auth"], payload,
                                vapid_for(self.state_dir), push_contact(),
                                urgency=urgency)
        except Exception as exc:
            # The type only: an endpoint is a bearer URL and must stay out of logs.
            sys.stderr.write(f"[nth_web push] send to {_host_of(endpoint)} failed: "
                             f"{type(exc).__name__}\n")
            status = 0
        if status in (404, 410):
            self._endpoint_backoff.pop(endpoint, None)
        elif status == 0 or status == 429 or status >= 500:
            # Transient: rest this endpoint for a minute instead of hammering
            # a push service that is already struggling.
            self._endpoint_backoff[endpoint] = time.monotonic() + 60
        elif status >= 400:
            sys.stderr.write(f"[nth_web push] {_host_of(endpoint)} refused a push "
                             f"(HTTP {status})\n")
        return status


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


def _save_state(db: sqlite3.Connection, sub: Dict[str, Any], state: SubState) -> None:
    sub["last_sent_at"] = state.last_sent_at
    sub["pending_count"] = state.pending_count
    sub["pending_sender"] = state.pending_sender
    db.execute("UPDATE push_subscriptions SET last_sent_at = ?, pending_count = ?, "
               "pending_sender = ? WHERE channel = ? AND endpoint = ?",
               (state.last_sent_at, state.pending_count, state.pending_sender,
                sub["channel"], sub["endpoint"]))
