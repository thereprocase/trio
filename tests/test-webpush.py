"""Tests for phone notifications (server/nth_webpush.py and its nth_web routes).

What is proven here, and why each matters:

  * ENCRYPTION reproduces RFC 8291 Appendix A byte for byte, and an
    independent receiver-side decryption opens a fresh random message. Without
    the vector, "the phone shows nothing" is the only symptom of a wrong HKDF
    label, and it is indistinguishable from a dozen other failures.
  * VAPID produces a JWT a verifier accepts, with the claims RFC 8292 requires,
    and the key file is created 0600 and reused rather than regenerated
    (regenerating would orphan every subscription).
  * The FREQUENCY function decides every mode, bangs crossing filters, and
    never notifying anyone about their own message.
  * The SSRF ALLOWLIST rejects everything that is not a known push service,
    including the look-alike hosts a suffix match gets wrong, and redirects
    are not followed.
  * The DISPATCHER sends what the function decides, never replays history on
    start, and drops a subscription the push service reports gone (410).
  * The HTTP SURFACE: subscribe tiers (pending refused, every named tier
    allowed), validation, status, unsubscribe, cross-origin refusal, and the
    manifest / service worker / icons served with the right headers.

Usage: PY=~/.claude/nth/venv/bin/python python tests/test-webpush.py
"""
import http.server
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="nth_webpush_")
os.environ["NTH_HOME"] = _TMP          # isolate before anything reads it
os.environ.pop("NTH_PUSH_CONTACT", None)

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_webpush as npush  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + f": {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


if not npush.available():
    print("SKIP: cryptography is not installed in this interpreter; set PY to the nth venv")
    # Still exercise the pure parts, which must work without it.

b64d, b64e = npush.b64url_decode, npush.b64url_encode

# ───────── SSRF allowlist (pure) ─────────
ALLOWED = [
    "https://fcm.googleapis.com/fcm/send/abc123",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAAA",
    "https://web.push.apple.com/QGx5Zz",
    "https://api.push.apple.com/3/device/abc",
    "https://wns2-par02p.notify.windows.com/w/?token=AwYAAAB",
    "https://fcm.googleapis.com:443/fcm/send/abc",
    "https://FCM.GoogleAPIs.com/fcm/send/abc",
]
REJECTED = [
    "http://fcm.googleapis.com/fcm/send/abc",                # not https
    "https://example.com/push",                               # unknown host
    "https://fcm.googleapis.com.evil.example.com/x",          # suffix trick
    "https://evilpush.apple.com/x",                           # no dot boundary
    "https://push.apple.com/x",                               # bare suffix
    "https://notify.windows.com/x",                           # bare suffix
    "https://fcm.googleapis.com:8443/fcm/send/abc",           # odd port
    "https://user:pw@fcm.googleapis.com/fcm/send/abc",        # userinfo
    "https://fcm.googleapis.com@127.0.0.1/x",                 # userinfo smuggle
    "https://127.0.0.1/push",
    "https://localhost/push",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/push",
    "file:///etc/passwd",
    "https://fcm.googleapis.com/fcm/send/" + "a" * 2000,      # overlong
    "https://fcm.googleapis.com/fcm send",                    # whitespace
    "",
    None,
]
for url in ALLOWED:
    check(f"ssrf: allows {url[:48]}", npush.endpoint_allowed(url))
for url in REJECTED:
    check(f"ssrf: rejects {str(url)[:48]!r}", not npush.endpoint_allowed(url))

# A redirect from a "push service" must not carry our POST to another host.
class _Redirector(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/internal")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


_rs = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
threading.Thread(target=_rs.serve_forever, daemon=True).start()
try:
    req = urllib.request.Request(f"http://127.0.0.1:{_rs.server_address[1]}/x",
                                 data=b"x", method="POST")
    try:
        npush._OPENER.open(req, timeout=5)
        followed = True
    except urllib.error.HTTPError as e:
        followed = e.code != 302
    check("ssrf: the push opener does not follow redirects", not followed)
finally:
    _rs.shutdown()

# ───────── Frequency function (pure) ─────────
ME, NAME = "_op_g_bob_abc123", "bob-guest"
OTHER = "agent-7"


def msg(content="hello", sender=OTHER, **kw):
    base = {"member_id": sender, "member_name": "Ada", "content": content,
            "mentions": [], "bangs": [], "recipients": "[]", "retracted_at": None}
    base.update(kw)
    return base


S0 = npush.SubState()
T = 1_000_000.0
D = npush.decide

check("freq: off never notifies", not D("off", msg(), ME, NAME, S0, T).send)
check("freq: off ignores even a bang", not D("off", msg("!all now"), ME, NAME, S0, T).send)
check("freq: unknown mode never notifies", not D("loud", msg(), ME, NAME, S0, T).send)
check("freq: all notifies on a plain message", D("all", msg(), ME, NAME, S0, T).send)
for mode in ("all", "mentions", "every5m"):
    own = D(mode, msg("@all !all mine", sender=ME), ME, NAME, S0, T)
    check(f"freq: {mode} never notifies about your own message", not own.send and own.state == S0)
check("freq: retracted message is silent",
      not D("all", msg(retracted_at="2026-01-01T00:00:00Z"), ME, NAME, S0, T).send)
check("freq: hub lifecycle notice is silent",
      not D("all", msg("[claimed #4 by Ada]"), ME, NAME, S0, T).send)
check("freq: a bracketed word that is not a notice still notifies",
      D("all", msg("[draft] see the plan"), ME, NAME, S0, T).send)
check("freq: a DM between two others is never pushed",
      not D("all", msg(recipients=json.dumps(["agent-9"])), ME, NAME, S0, T).send)

check("freq: mentions skips an untargeted message", not D("mentions", msg(), ME, NAME, S0, T).send)
check("freq: mentions fires on @name", D("mentions", msg("@bob-guest look"), ME, NAME, S0, T).send)
check("freq: mentions matches the name case-insensitively",
      D("mentions", msg("@BOB-GUEST look"), ME, NAME, S0, T).send)
check("freq: mentions fires on @member_id", D("mentions", msg(f"@{ME} look"), ME, NAME, S0, T).send)
check("freq: mentions fires on @all", D("mentions", msg("@all standup"), ME, NAME, S0, T).send)
check("freq: mentions fires on the stored mentions array",
      D("mentions", msg("hey you", mentions=[ME]), ME, NAME, S0, T).send)
check("freq: mentions fires on a DM addressed to you",
      D("mentions", msg("psst", recipients=json.dumps([ME])), ME, NAME, S0, T).send)
check("freq: @bobby does not mention bob",
      not D("mentions", msg("@bob-guestly"), ME, NAME, S0, T).send)
check("freq: a bang for someone else does not cross your filter",
      not D("mentions", msg("!ada fix it"), ME, NAME, S0, T).send)

bang = D("mentions", msg("!bob-guest the build is on fire"), ME, NAME, S0, T)
check("freq: a bang crosses the mentions filter", bang.send and bang.kind == "bang")
check("freq: !all crosses the mentions filter", D("mentions", msg("!all stop"), ME, NAME, S0, T).send)
check("freq: stored bangs array crosses the filter",
      D("mentions", msg("stop", bangs=[ME]), ME, NAME, S0, T).send)

# every5m: first message after quiet sends at once as a 1-message summary.
d1 = D("every5m", msg("one"), ME, NAME, S0, T)
check("freq: every5m sends the first message after a quiet spell",
      d1.send and d1.kind == "digest" and d1.count == 1 and d1.sender == "Ada")
d2 = D("every5m", msg("two", member_name="Cy"), ME, NAME, d1.state, T + 10)
check("freq: every5m holds a second message inside the window", not d2.send)
d3 = D("every5m", msg("three", member_name="Di"), ME, NAME, d2.state, T + 20)
check("freq: every5m counts what it holds",
      d3.state.pending_count == 2 and d3.state.pending_sender == "Di")
d_bang = D("every5m", msg("!all now"), ME, NAME, d3.state, T + 30)
check("freq: a bang crosses every5m immediately",
      d_bang.send and d_bang.kind == "bang" and d_bang.state == d3.state)
check("freq: flush_due waits for the window",
      not npush.flush_due("every5m", d3.state, T + 299).send)
f = npush.flush_due("every5m", d3.state, T + 300)
check("freq: flush_due sends the held summary after five minutes",
      f.send and f.kind == "digest" and f.count == 2 and f.sender == "Di"
      and f.state.pending_count == 0 and f.state.last_sent_at == T + 300)
check("freq: flush_due does nothing with nothing held",
      not npush.flush_due("every5m", f.state, T + 9999).send)
check("freq: flush_due does nothing for other modes",
      not npush.flush_due("all", d3.state, T + 9999).send)
d4 = D("every5m", msg("late"), ME, NAME, d3.state, T + 301)
check("freq: a message after the window sends the summary including itself",
      d4.send and d4.kind == "digest" and d4.count == 3)

p = npush.build_payload("ops", d4)
check("payload: digest names the count and latest sender",
      p["title"] == "#ops — 3 new messages" and "Ada" in p["body"])
p = npush.build_payload("ops", bang, msg("!bob-guest the build is on fire"))
check("payload: per-channel tag and channel URL",
      p["tag"] == "nth-ops" and p["url"] == "/?channel=ops" and p["channel"] == "ops")
check("payload: bang is marked urgent", p["title"].startswith("Urgent:"))
p = npush.build_payload("ops", npush.Decision(True, "message"), msg("x" * 1000))
check("payload: body is truncated", len(p["body"]) <= npush.MAX_BODY_CHARS)

if not npush.available():
    print()
    print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s) (crypto parts skipped)")
    sys.exit(1 if failures else 0)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

# ───────── RFC 8291 Appendix A ─────────
RFC_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_AS_PUBLIC = ("BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
                 "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_UA_PUBLIC = ("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcx"
                 "aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_RESULT = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
              "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
              "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def private_from(b64):
    return ec.derive_private_key(int.from_bytes(b64d(b64), "big"), ec.SECP256R1())


as_priv = private_from(RFC_AS_PRIVATE)
check("rfc8291: the sender key in the vector matches its public key",
      as_priv.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
      == b64d(RFC_AS_PUBLIC))
out = npush.encrypt_aes128gcm(RFC_PLAINTEXT, b64d(RFC_UA_PUBLIC), b64d(RFC_AUTH),
                              salt=b64d(RFC_SALT), as_private=as_priv)
check("rfc8291: Appendix A message reproduced byte for byte", b64e(out) == RFC_RESULT,
      b64e(out))
check("rfc8291: 86-octet header (salt, rs=4096, keyid length 65, as_public)",
      out[:16] == b64d(RFC_SALT) and struct.unpack("!IB", out[16:21]) == (4096, 65)
      and out[21:86] == b64d(RFC_AS_PUBLIC))


def receiver_decrypt(body, ua_private, auth_secret):
    """The user agent's side of RFC 8291, written independently of the sender."""
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    import hashlib
    import hmac

    def h(k, d):
        return hmac.new(k, d, hashlib.sha256).digest()
    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = h(h(auth_secret, shared), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = h(salt, ikm)
    cek = h(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = h(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert rs == 4096
    assert plain.endswith(b"\x02")
    return plain[:-1]


check("rfc8291: an independent receiver decrypts the vector",
      receiver_decrypt(out, private_from(RFC_UA_PRIVATE), b64d(RFC_AUTH)) == RFC_PLAINTEXT)
fresh = npush.encrypt_aes128gcm(b'{"title":"t"}', b64d(RFC_UA_PUBLIC), b64d(RFC_AUTH))
check("rfc8291: production calls use a fresh salt and key", fresh[:16] != b64d(RFC_SALT))
check("rfc8291: a fresh message still decrypts",
      receiver_decrypt(fresh, private_from(RFC_UA_PRIVATE), b64d(RFC_AUTH)) == b'{"title":"t"}')

# ───────── VAPID ─────────
state_dir = Path(_TMP) / "vapid"
keys = npush.VapidKeys.load_or_create(state_dir)
key_file = state_dir / npush.VAPID_KEY_FILENAME
check("vapid: key file created on first use", key_file.exists())
check("vapid: key file is mode 0600", stat.S_IMODE(key_file.stat().st_mode) == 0o600,
      oct(stat.S_IMODE(key_file.stat().st_mode)))
check("vapid: no temp files left behind",
      [p.name for p in state_dir.iterdir()] == [npush.VAPID_KEY_FILENAME])
again = npush.VapidKeys.load_or_create(state_dir)
check("vapid: the key is reused, never regenerated", again.public_b64 == keys.public_b64)
check("vapid: public key is a 65-byte uncompressed point",
      len(keys.public_bytes) == 65 and keys.public_bytes[0] == 4)
os.chmod(key_file, 0o644)
npush.VapidKeys.load_or_create(state_dir)
check("vapid: loose permissions are tightened back to 0600",
      stat.S_IMODE(key_file.stat().st_mode) == 0o600)

now = 1_800_000_000
endpoint = "https://fcm.googleapis.com/fcm/send/abc"
header = keys.authorization(endpoint, "mailto:admin@example.com", now=now)
check("vapid: Authorization is 'vapid t=<jwt>, k=<key>'",
      header.startswith("vapid t=") and header.endswith(", k=" + keys.public_b64))
token = header[len("vapid t="):header.index(", k=")]
parts = token.split(".")
check("vapid: the JWT has three parts", len(parts) == 3)
jwt_header = json.loads(b64d(parts[0]))
check("vapid: JWT header is ES256", jwt_header == {"typ": "JWT", "alg": "ES256"})
claims = npush.verify_jwt(token, keys.public_bytes)
check("vapid: signature verifies against the advertised key", True)
check("vapid: aud is the push service origin", claims["aud"] == "https://fcm.googleapis.com")
check("vapid: exp is in the future and at most 24h out",
      now < claims["exp"] <= now + 24 * 3600)
check("vapid: sub is the contact", claims["sub"] == "mailto:admin@example.com")
check("vapid: signature is raw 64-byte r||s", len(b64d(parts[2])) == 64)
tampered = parts[0] + "." + b64e(json.dumps({**claims, "aud": "https://example.com"}).encode()) + "." + parts[2]
try:
    npush.verify_jwt(tampered, keys.public_bytes)
    check("vapid: a tampered JWT fails verification", False)
except npush.InvalidSignature:
    check("vapid: a tampered JWT fails verification", True)
other = npush.VapidKeys(ec.generate_private_key(ec.SECP256R1()))
try:
    npush.verify_jwt(token, other.public_bytes)
    check("vapid: another key does not verify the JWT", False)
except npush.InvalidSignature:
    check("vapid: another key does not verify the JWT", True)
check("vapid: contact defaults to the placeholder", npush.push_contact() == "mailto:admin@example.com")
os.environ["NTH_PUSH_CONTACT"] = "mailto:ops@example.com"
check("vapid: NTH_PUSH_CONTACT overrides the contact", npush.push_contact() == "mailto:ops@example.com")
os.environ["NTH_PUSH_CONTACT"] = "not-a-uri"
check("vapid: a malformed contact falls back", npush.push_contact() == "mailto:admin@example.com")
os.environ.pop("NTH_PUSH_CONTACT")

# ───────── Subscription validation ─────────
ua_priv = ec.generate_private_key(ec.SECP256R1())
UA_PUBLIC = b64e(ua_priv.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
UA_AUTH = b64e(os.urandom(16))
GOOD_SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/device-1",
            "keys": {"p256dh": UA_PUBLIC, "auth": UA_AUTH}}
check("validate: a real subscription passes",
      npush.validate_subscription(GOOD_SUB)[0] == GOOD_SUB["endpoint"])
for label, bad in (
    ("internal endpoint", {**GOOD_SUB, "endpoint": "https://127.0.0.1/x"}),
    ("missing keys", {"endpoint": GOOD_SUB["endpoint"]}),
    ("short auth", {**GOOD_SUB, "keys": {"p256dh": UA_PUBLIC, "auth": b64e(b"x" * 8)}}),
    ("off-curve point", {**GOOD_SUB, "keys": {"p256dh": b64e(b"\x04" + b"\x01" * 64), "auth": UA_AUTH}}),
    ("non-base64 key", {**GOOD_SUB, "keys": {"p256dh": "!!!", "auth": UA_AUTH}}),
    ("not an object", "https://fcm.googleapis.com/x"),
):
    try:
        npush.validate_subscription(bad)
        check(f"validate: rejects {label}", False)
    except ValueError:
        check(f"validate: rejects {label}", True)

# ───────── send_push: real encryption, captured request ─────────
captured = {}


class _CaptureOpener:
    def open(self, req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data

        class _Resp:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _Resp()


status = npush.send_push(GOOD_SUB["endpoint"], UA_PUBLIC, UA_AUTH,
                         {"title": "#ops — Ada", "body": "hi"}, keys,
                         "mailto:admin@example.com", urgency="high",
                         opener=_CaptureOpener())
h = captured.get("headers", {})
check("send: returns the push service status", status == 201)
check("send: aes128gcm content coding", h.get("content-encoding") == "aes128gcm")
check("send: TTL header", h.get("ttl") == str(npush.PUSH_TTL_S))
check("send: Urgency header", h.get("urgency") == "high")
check("send: VAPID authorization", (h.get("authorization") or "").startswith("vapid t="))
check("send: the subscriber can decrypt the payload",
      json.loads(receiver_decrypt(captured["body"], ua_priv, b64d(UA_AUTH)))
      == {"title": "#ops — Ada", "body": "hi"})
captured.clear()
status = npush.send_push("https://127.0.0.1/x", UA_PUBLIC, UA_AUTH, {}, keys,
                         "mailto:admin@example.com", opener=_CaptureOpener())
check("send: refuses a non-allowlisted endpoint without any request",
      status == 0 and not captured)

# ───────── Dispatcher against a real hub DB ─────────
import nth_server as srv  # noqa: E402
import nth_web as web     # noqa: E402

srv.DB_DIR = Path(_TMP)
srv.DB_PATH = Path(_TMP) / "nth.db"
web.ATTACH_DIR = Path(_TMP) / "attachments"
r = json.loads(srv.nth_connect(summary="t", name="Ada", channel="pushtest"))
CH, ADA = r["channel"], r["member_id"]
_mig = __import__("sqlite3").connect(str(srv.DB_PATH))
web.ensure_ask_columns(_mig)
npush.ensure_push_table(_mig)
_mig.commit()
_mig.close()

srv.nth_send(channel=CH, member_id=ADA, message="history before anyone subscribed")

sent = []
clock = [T]


lock_held_during_send = []


def fake_sender(endpoint, p256dh, auth_secret, payload, vapid, contact, urgency="normal"):
    sent.append((endpoint, payload, urgency))
    # A send can take the push service's full timeout. If the dispatcher were
    # still inside its write transaction here, every agent post would queue
    # behind the network; prove a writer can get in immediately.
    probe = __import__("sqlite3").connect(str(srv.DB_PATH), timeout=0.2)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001
        lock_held_during_send.append(repr(exc))
    finally:
        probe.close()
    return fake_sender.status


fake_sender.status = 201
disp = npush.PushDispatcher(srv.DB_PATH, Path(_TMP), sender=fake_sender, clock=lambda: clock[0])
disp.tick()
check("dispatch: first tick seeds the high-water mark and sends nothing", sent == [])

import sqlite3  # noqa: E402
db = sqlite3.connect(str(srv.DB_PATH))
for i, mode in enumerate(("all", "mentions", "every5m", "off")):
    npush.upsert_subscription(db, channel=CH, endpoint=f"https://fcm.googleapis.com/fcm/send/d{i}",
                              p256dh=UA_PUBLIC, auth=UA_AUTH, member_id=f"_op_g_u{i}_x",
                              member_name=f"u{i}-guest", mode=mode)
db.commit()
disp.tick()
check("dispatch: subscribing does not replay older messages", sent == [])

srv.nth_send(channel=CH, member_id=ADA, message="plain update")
disp.tick()
eps = sorted(e for e, _p, _u in sent)
check("dispatch: plain message reaches 'all' and the every5m summary only",
      eps == ["https://fcm.googleapis.com/fcm/send/d0", "https://fcm.googleapis.com/fcm/send/d2"],
      str(eps))
payload = next(p for e, p, _u in sent if e.endswith("/d0"))
check("dispatch: payload carries channel tag and URL",
      payload["tag"] == f"nth-{CH}" and payload["url"] == f"/?channel={CH}")
sent.clear()

srv.nth_send(channel=CH, member_id=ADA, message="@u1-guest can you look")
disp.tick()
eps = sorted(e for e, _p, _u in sent)
check("dispatch: a mention reaches the mentions subscriber",
      "https://fcm.googleapis.com/fcm/send/d1" in eps)
check("dispatch: every5m holds the second message inside the window",
      "https://fcm.googleapis.com/fcm/send/d2" not in eps)
check("dispatch: 'off' never receives anything",
      "https://fcm.googleapis.com/fcm/send/d3" not in eps)
sent.clear()

srv.nth_send(channel=CH, member_id=ADA, message="!all deploy is broken")
disp.tick()
eps = sorted(e for e, _p, _u in sent)
check("dispatch: !all reaches all, mentions and every5m at once",
      eps == [f"https://fcm.googleapis.com/fcm/send/d{i}" for i in range(3)], str(eps))
check("dispatch: bangs go out with high urgency", all(u == "high" for _e, _p, u in sent))
sent.clear()

clock[0] = T + npush.DIGEST_INTERVAL_S + 1
disp.tick()
check("dispatch: the held every5m summary flushes after the window",
      [e for e, _p, _u in sent] == ["https://fcm.googleapis.com/fcm/send/d2"]
      and sent[0][1]["title"].endswith("1 new message"))
sent.clear()

fake_sender.status = 410
srv.nth_send(channel=CH, member_id=ADA, message="anyone there")
disp.tick()
left = {r[0] for r in db.execute("SELECT endpoint FROM push_subscriptions")}
check("dispatch: a 410 drops that subscription",
      "https://fcm.googleapis.com/fcm/send/d0" not in left
      and "https://fcm.googleapis.com/fcm/send/d3" in left)
fake_sender.status = 201
check("dispatch: the DB write lock is free while a push is in flight",
      not lock_held_during_send, "; ".join(lock_held_during_send[:2]))


def boom(*a, **k):
    raise RuntimeError("push service exploded")


disp_bad = npush.PushDispatcher(srv.DB_PATH, Path(_TMP), sender=boom, clock=lambda: clock[0])
disp_bad.tick()
srv.nth_send(channel=CH, member_id=ADA, message="@all still alive?")
try:
    disp_bad.tick()
    check("dispatch: a sender that raises does not escape the tick", True)
except Exception as exc:  # noqa: BLE001
    check("dispatch: a sender that raises does not escape the tick", False, repr(exc))
db.execute("DELETE FROM push_subscriptions")
db.commit()
db.close()

# ───────── HTTP surface ─────────
hub = web.EventHub(srv.DB_PATH, CH)
server = None


def call(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def as_json(raw):
    try:
        return json.loads(raw.decode())
    except ValueError:
        return {}


try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH
    web.NthWebHandler.landing_mode = True
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    st, hd, raw = call(port, "GET", "/manifest.webmanifest")
    man = as_json(raw)
    check("http: manifest served", st == 200)
    check("http: manifest content type", hd.get("Content-Type") == "application/manifest+json")
    check("http: manifest is standalone at scope /",
          man.get("display") == "standalone" and man.get("scope") == "/" and man.get("start_url") == "/")
    sizes = {(i["sizes"], i.get("purpose", "any")) for i in man.get("icons", [])}
    check("http: manifest has 192/512 any and maskable icons",
          {("192x192", "any"), ("512x512", "any"), ("192x192", "maskable"),
           ("512x512", "maskable")} <= sizes)
    for icon in man.get("icons", []):
        st, hd, raw = call(port, "GET", icon["src"])
        w, hgt = struct.unpack(">II", raw[16:24]) if len(raw) > 24 else (0, 0)
        check(f"http: {icon['src']} is a {icon['sizes']} PNG",
              st == 200 and hd.get("Content-Type") == "image/png"
              and raw[:8] == b"\x89PNG\r\n\x1a\n" and f"{w}x{hgt}" == icon["sizes"])
    st, hd, raw = call(port, "GET", "/apple-touch-icon.png")
    check("http: apple-touch-icon at the root is a 180px PNG",
          st == 200 and struct.unpack(">II", raw[16:24]) == (180, 180))
    st, hd, raw = call(port, "GET", "/sw.js")
    check("http: sw.js served as JavaScript",
          st == 200 and hd.get("Content-Type", "").startswith("text/javascript"))
    check("http: sw.js allows root scope", hd.get("Service-Worker-Allowed") == "/")
    check("http: sw.js is revalidated every time", hd.get("Cache-Control") == "no-cache")
    check("http: sw.js has push + notificationclick and no fetch handler",
          b"'push'" in raw and b"'notificationclick'" in raw and b"'fetch'" not in raw)
    st, _hd, raw = call(port, "GET", "/")
    page = raw.decode("utf-8", "replace")
    check("http: page links the manifest", '<link rel="manifest" href="/manifest.webmanifest">' in page)
    check("http: page has theme-color and apple web-app tags",
          'name="theme-color"' in page and 'apple-mobile-web-app-capable' in page
          and 'rel="apple-touch-icon"' in page)

    st, _hd, raw = call(port, "GET", "/api/push/vapid-public-key")
    vk = as_json(raw)
    check("http: VAPID public key served", st == 200 and vk.get("enabled") is True
          and len(b64d(vk.get("publicKey", ""))) == 65)
    key_path = Path(_TMP) / npush.VAPID_KEY_FILENAME
    check("http: hub key stored beside nth.db, mode 0600",
          key_path.exists() and stat.S_IMODE(key_path.stat().st_mode) == 0o600)
    check("http: the private key never appears in the response", b"PRIVATE" not in raw)

    SUB = {"subscription": GOOD_SUB, "channel": CH, "mode": "mentions"}
    _real = web.NthWebHandler._resolve_identity
    tiers = (
        (web.IDENTITY_SOURCE_PENDING, "", 403),
        (web.IDENTITY_SOURCE_GUEST, "gus", 200),
        (web.IDENTITY_SOURCE_MEMBER, "mia", 200),
        (web.IDENTITY_SOURCE_TAILSCALE, "owner", 200),
        (web.IDENTITY_SOURCE_LOOPBACK, "local", 200),
    )
    try:
        for source, name, want in tiers:
            ident = web.OperatorIdentity(member_id=f"_op_x_{source}", name=name, source=source,
                                         login=f"{name}@example.com")
            web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)
            sub = {**SUB, "subscription": {**GOOD_SUB, "endpoint": GOOD_SUB["endpoint"] + source}}
            st, _hd, raw = call(port, "POST", "/api/push/subscribe", sub)
            check(f"auth: {source} subscribe -> {want}", st == want, f"{st} {raw[:120]!r}")
        # A guest that is tailnet-verified is still a guest tier: allowed.
        ident = web.OperatorIdentity(member_id="_op_g_tv_x", name="tv", source=web.IDENTITY_SOURCE_GUEST,
                                     login="tv@example.com", tailnet_verified=True)
        web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe",
                             {**SUB, "subscription": {**GOOD_SUB, "endpoint": GOOD_SUB["endpoint"] + "tv"}})
        check("auth: tailnet-verified guest may subscribe", st == 200)

        ident = web.OperatorIdentity(member_id="_op_g_val_x", name="val", source=web.IDENTITY_SOURCE_GUEST)
        web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)
        for label, body, want in (
            ("internal endpoint", {**SUB, "subscription": {**GOOD_SUB, "endpoint": "https://169.254.169.254/x"}}, 400),
            ("http endpoint", {**SUB, "subscription": {**GOOD_SUB, "endpoint": "http://fcm.googleapis.com/x"}}, 400),
            ("unknown mode", {**SUB, "mode": "loud"}, 400),
            ("missing channel", {**SUB, "channel": ""}, 400),
            ("unknown channel", {**SUB, "channel": "no-such-chan"}, 404),
        ):
            st, _hd, _raw = call(port, "POST", "/api/push/subscribe", body)
            check(f"subscribe: rejects {label} ({want})", st == want, str(st))
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe", SUB,
                             headers={"Origin": "https://evil.example.com"})
        check("subscribe: cross-origin POST refused", st == 403)

        st, _hd, _raw = call(port, "POST", "/api/push/subscribe", {**SUB, "mode": "every5m"})
        check("subscribe: named guest can subscribe", st == 200)
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        status = as_json(raw)
        check("status: shows this identity's mode for the channel",
              st == 200 and status.get("subscriptions") == [
                  {"endpoint": GOOD_SUB["endpoint"], "mode": "every5m"}], str(status))
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe", {**SUB, "mode": "all"})
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: changing the mode replaces, never duplicates",
              as_json(raw).get("subscriptions") == [{"endpoint": GOOD_SUB["endpoint"], "mode": "all"}])

        thief = web.OperatorIdentity(member_id="_op_g_thief_x", name="thief", source=web.IDENTITY_SOURCE_GUEST)
        web.NthWebHandler._resolve_identity = lambda self, _i=thief: (None, _i, False)
        st, _hd, raw = call(port, "POST", "/api/push/unsubscribe",
                            {"endpoint": GOOD_SUB["endpoint"], "channel": CH})
        check("unsubscribe: another identity cannot remove it", as_json(raw).get("removed") == 0)
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: another identity does not see it", as_json(raw).get("subscriptions") == [])

        web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)
        st, _hd, raw = call(port, "POST", "/api/push/unsubscribe",
                            {"endpoint": GOOD_SUB["endpoint"], "channel": CH})
        check("unsubscribe: the owner removes it", st == 200 and as_json(raw).get("removed") == 1)
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: empty after unsubscribe", as_json(raw).get("subscriptions") == [])

        pending = web.OperatorIdentity(member_id="_op_p_x", name="", source=web.IDENTITY_SOURCE_PENDING)
        web.NthWebHandler._resolve_identity = lambda self, _i=pending: (None, _i, False)
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: a pending visitor is told it is not named",
              st == 200 and as_json(raw).get("named") is False)
        st, _hd, raw = call(port, "POST", "/api/push/unsubscribe", {"endpoint": GOOD_SUB["endpoint"]})
        check("unsubscribe: a pending visitor is refused", st == 403)
    finally:
        web.NthWebHandler._resolve_identity = _real
except OSError as e:
    print(f"SKIP: http part (could not start server: {e})", file=sys.stderr)
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    hub.stop()
    web.NthWebHandler.landing_mode = False

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
