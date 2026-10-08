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
p = npush.build_payload("ops", npush.Decision(True, "message"), msg("x" * 1000), show_text=True)
check("payload: body is truncated", len(p["body"]) <= npush.MAX_BODY_CHARS)

# Message text stays off the lock screen unless the device opted in.
SECRET = "the deploy password is hunter2"
p = npush.build_payload("ops", npush.Decision(True, "message"), msg(SECRET))
check("payload: hidden by default -- the body is the neutral line",
      p["body"] == npush.HIDDEN_BODY == "New message")
check("payload: hidden by default -- the title still names channel and sender",
      p["title"] == "#ops — Ada")
check("payload: hidden by default -- no part of the text anywhere in the payload",
      "hunter2" not in json.dumps(p) and "deploy" not in json.dumps(p))
p = npush.build_payload("ops", npush.Decision(True, "message"),
                        msg(SECRET, recipients=json.dumps([ME])))
check("payload: a hidden DM names the DM and sender only",
      p["title"] == "DM — Ada" and p["body"] == npush.HIDDEN_BODY)
p = npush.build_payload("ops", bang, msg("!bob-guest " + SECRET))
check("payload: a hidden bang is still marked urgent and still hides the text",
      p["title"].startswith("Urgent:") and "hunter2" not in json.dumps(p))
p = npush.build_payload("ops", npush.Decision(True, "message"), msg(SECRET), show_text=True)
check("payload: show_text puts the message in the body", p["body"] == SECRET)
check("payload: a digest is the same hidden or shown",
      npush.build_payload("ops", d4) == npush.build_payload("ops", d4, show_text=True))
tp = npush.test_payload("ops")
check("payload: the test notification names the channel and says what it proves",
      tp["title"] == "Test notification from #ops"
      and tp["body"] == "If you can read this, notifications reach this device."
      and tp["url"] == "/?channel=ops")
check("payload: the test notification never replaces a real one (own tag)",
      tp["tag"] != npush.build_payload("ops", d4)["tag"])

# The test button's limiter, on a hand-driven clock.
FCM, MOZ = "https://fcm.googleapis.com/fcm/send/", "https://updates.push.services.mozilla.com/wpush/v2/"
G, TR = npush.TIER_GUEST, npush.TIER_TRUSTED
_lt = [100.0]
lim = npush.TestPushLimiter(interval_s=10.0, clock=lambda: _lt[0])
check("test limit: the first press goes through",
      lim.take(member_id="m1", endpoint=FCM + "a", tier=TR) == 0)
check("test limit: a second press inside 10 s waits",
      9.9 < lim.take(member_id="m1", endpoint=FCM + "a", tier=TR) <= 10.0)
check("test limit: the same member on a fresh endpoint at the same service waits too",
      lim.take(member_id="m1", endpoint=FCM + "a-fresh", tier=TR) > 9.9)
check("test limit: a trailing-dot spelling of the host is the same service",
      lim.take(member_id="m1", endpoint="https://FCM.googleapis.com./fcm/send/x", tier=TR) > 9.9)
check("test limit: the same member at another push service is not held up",
      lim.take(member_id="m1", endpoint=MOZ + "a", tier=TR) == 0)
check("test limit: another member is not held up",
      lim.take(member_id="m2", endpoint=FCM + "b", tier=TR) == 0)
_lt[0] += 4
check("test limit: the wait counts down",
      5.9 < lim.take(member_id="m1", endpoint=FCM + "a", tier=TR) <= 6.0)
_lt[0] += 6
check("test limit: allowed again once the interval has passed",
      lim.take(member_id="m1", endpoint=FCM + "a", tier=TR) == 0)

# Churn: a guest minting identities and endpoints still meets the guest tier's
# budget, and the trusted tier keeps its own.
_lt = [100.0]
lim = npush.TestPushLimiter(clock=lambda: _lt[0])
g_burst, g_rate = npush.TEST_PUSH_TIER_BUDGET[G]
taken = [lim.take(member_id=f"_op_g_churn{i}_x", endpoint=f"{FCM}churn{i}", tier=G)
         for i in range(g_burst + 3)]
check(f"test limit: guests churning endpoints get {g_burst} tests, then wait",
      taken[:g_burst] == [0.0] * g_burst and all(w > 0 for w in taken[g_burst:]), str(taken))
check("test limit: the trusted tier is unaffected by an exhausted guest tier",
      lim.take(member_id="_op_t_owner", endpoint=FCM + "owner", tier=TR) == 0)
check("test limit: an unknown tier gets the guest budget, never an unlimited one",
      lim.take(member_id="_op_q_x", endpoint=FCM + "q", tier="mystery") > 0)
_lt[0] += 60.0 / g_rate
check("test limit: the guest budget refills over time",
      lim.take(member_id="_op_g_late_x", endpoint=FCM + "late", tier=G) == 0)
check("test limit: a refused press is not charged (the refill bought exactly one)",
      lim.take(member_id="_op_g_later_x", endpoint=FCM + "later", tier=G) > 0)

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
for i, mode in enumerate(("all", "mentions", "every5m", "all")):
    npush.upsert_subscription(db, channel=CH, endpoint=f"https://fcm.googleapis.com/fcm/send/d{i}",
                              p256dh=UA_PUBLIC, auth=UA_AUTH, member_id=f"_op_g_u{i}_x",
                              member_name=f"u{i}-guest", mode=mode)
# An 'off' row can no longer be created through subscribe; one left by an
# older build must still be ignored by the dispatcher.
db.execute("UPDATE push_subscriptions SET mode = 'off' WHERE endpoint LIKE '%/d3'")
db.commit()
try:
    npush.upsert_subscription(db, channel=CH, endpoint="https://fcm.googleapis.com/fcm/send/off",
                              p256dh=UA_PUBLIC, auth=UA_AUTH, member_id="_op_g_off_x",
                              member_name="off-guest", mode="off")
    check("store: mode off is refused at subscribe (off means unsubscribe)", False)
except ValueError:
    check("store: mode off is refused at subscribe (off means unsubscribe)", True)
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

# ───────── Review fixes: delivery guarantees and bounds ─────────
EP = "https://fcm.googleapis.com/fcm/send/"


def add_sub(n, mode="all", member=None, tier=npush.TIER_GUEST, channel=None, show_text=True):
    # show_text defaults to True HERE ONLY: the delivery tests below tell
    # messages apart by their text. The product default (hidden) has its own
    # tests further down.
    conn = sqlite3.connect(str(srv.DB_PATH))
    npush.upsert_subscription(conn, channel=channel or CH, endpoint=f"{EP}{n}",
                              p256dh=UA_PUBLIC, auth=UA_AUTH,
                              member_id=member or f"_op_g_{n}_x", member_name=f"{n}-guest",
                              mode=mode, tier=tier, show_text=show_text)
    conn.commit()
    conn.close()


def sub_row(n, channel=None):
    conn = sqlite3.connect(str(srv.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM push_subscriptions WHERE channel = ? AND endpoint = ?",
                            (channel or CH, f"{EP}{n}")).fetchone()
    finally:
        conn.close()


def clear_subs():
    conn = sqlite3.connect(str(srv.DB_PATH))
    conn.execute("DELETE FROM push_subscriptions")
    conn.commit()
    conn.close()


class Scripted:
    """A sender whose status per endpoint the test sets, recording calls."""

    def __init__(self):
        self.status, self.calls, self.delay = {}, [], 0.0
        self._lock = threading.Lock()

    def __call__(self, endpoint, p256dh, auth_secret, payload, vapid, contact, urgency="normal"):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls.append((endpoint, payload, urgency))
        return self.status.get(endpoint, 201)


def fresh(sender, **kw):
    d = npush.PushDispatcher(srv.DB_PATH, Path(_TMP), sender=sender,
                             clock=lambda: clock[0], **kw)
    d.tick()                       # seed the high-water mark
    return d


def say(text):
    srv.nth_send(channel=CH, member_id=ADA, message=text)


# 1. Lease loss stops delivery.
clear_subs()
add_sub("lease")
held = [True]
sc = Scripted()
d = fresh(sc, lease_check=lambda: held[0])
say("before the takeover")
d.tick()
check("lease: a holder sends", len(sc.calls) == 1)
held[0] = False
say("after the takeover")
check("lease: a tick after losing the lease sends nothing", d.tick() == 0 and len(sc.calls) == 1)
check("lease: the dispatcher stops itself", d.stopped)

lease = web.AgentControlLease(srv.DB_PATH)
check("lease: acquire on a fresh DB", lease.acquire() is None)
check("lease: lease_state is held while we hold it", lease.lease_state() == npush.LEASE_HELD)
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("UPDATE agent_control_lease SET holder = 'other-host:1:abcd' WHERE id = 1")
conn.commit()
conn.close()
check("lease: lease_state names the takeover", lease.lease_state() == npush.LEASE_LOST)
d2 = fresh(Scripted(), lease_check=lease.lease_state)
check("lease: a dispatcher wired to a lost lease stops on its first tick", d2.stopped)

# Our own row past its expiry is a pause, never a stop: renew() keeps the
# lease through a locked DB or a suspend, so delivery must come back.
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("DELETE FROM agent_control_lease")
conn.commit()
conn.close()
lease = web.AgentControlLease(srv.DB_PATH)
lease.acquire()
sc = Scripted()
d3 = fresh(sc, lease_check=lease.lease_state)
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("UPDATE agent_control_lease SET expires_at = ?", (time.time() - 1,))
conn.commit()
conn.close()
check("lease: our own expired row reads as expired", lease.lease_state() == npush.LEASE_EXPIRED)
say("while the lease is expired")
check("lease: an expired own lease skips the tick", d3.tick() == 0 and not sc.calls)
check("lease: an expired own lease leaves the dispatcher running", not d3.stopped)
check("lease: the hub renews its own lease", lease.renew())
d3.tick()
check("lease: delivery resumes after renewal, including the held message",
      not d3.stopped and [p["body"] for _e, p, _u in sc.calls] == ["while the lease is expired"])

# A lease lost in the middle of a long batch stops the remaining sends.
clear_subs()
for i in range(4):
    add_sub(f"mid{i}", member=f"_op_g_mid{i}_x")
answers = iter([npush.LEASE_HELD, npush.LEASE_HELD, npush.LEASE_HELD])
_recheck, _workers = npush.LEASE_RECHECK_S, npush.SEND_WORKERS
npush.LEASE_RECHECK_S, npush.SEND_WORKERS = 0.0, 1
try:
    sc = Scripted()
    d4 = fresh(sc, lease_check=lambda: next(answers, npush.LEASE_LOST))
    say("long batch")
    d4.tick()
finally:
    npush.LEASE_RECHECK_S, npush.SEND_WORKERS = _recheck, _workers
check(f"lease: a takeover mid-batch stops the remaining sends ({len(sc.calls)} of 4)",
      len(sc.calls) == 1 and d4.stopped)
_was_enabled = web.NthWebHandler._agent_control_enabled
web._PUSH_DISPATCHER = npush.PushDispatcher(srv.DB_PATH, Path(_TMP), sender=Scripted())
victim = web._PUSH_DISPATCHER
web._quiesce_agents()
check("lease: _quiesce_agents (the on_lost hook) stops the push dispatcher",
      victim.stopped and web._PUSH_DISPATCHER is None)
web.NthWebHandler._agent_control_enabled = _was_enabled
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("DELETE FROM agent_control_lease")
conn.commit()
conn.close()

# 2. Tier quotas: guests have their own small pool.
clear_subs()
_quotas = dict(npush.TIER_QUOTAS)
npush.TIER_QUOTAS[npush.TIER_GUEST] = (8, 3)
try:
    for i in range(3):
        add_sub(f"g{i}", member=f"_op_g_cookie{i}_x")
    try:
        add_sub("g3", member="_op_g_cookie3_x")
        check("quota: the guest pool is capped", False)
    except npush.SubscriptionLimit:
        check("quota: the guest pool is capped", True)
    add_sub("owner", member="_op_t_owner", tier=npush.TIER_TRUSTED)
    check("quota: a full guest pool leaves the owner room", sub_row("owner") is not None)
finally:
    npush.TIER_QUOTAS.update(_quotas)
check("quota: guests and trusted tiers map as intended",
      web._push_tier(web.OperatorIdentity("_op_g_x", "x", web.IDENTITY_SOURCE_GUEST)) == npush.TIER_GUEST
      and web._push_tier(web.OperatorIdentity("_op_g_y", "y", web.IDENTITY_SOURCE_GUEST,
                                              tailnet_verified=True)) == npush.TIER_GUEST
      and all(web._push_tier(web.OperatorIdentity("_op_z", "z", src)) == npush.TIER_TRUSTED
              for src in (web.IDENTITY_SOURCE_LOOPBACK, web.IDENTITY_SOURCE_TAILSCALE,
                          web.IDENTITY_SOURCE_MEMBER)))

# 7 + 10. Ownership and mode changes.
clear_subs()
add_sub("own", mode="every5m", member="_op_g_alice_x")
try:
    add_sub("own", mode="all", member="_op_g_mallory_x")
    check("upsert: another identity cannot take over a subscription", False)
except npush.SubscriptionConflict:
    check("upsert: another identity cannot take over a subscription",
          sub_row("own")["member_id"] == "_op_g_alice_x")
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("UPDATE push_subscriptions SET pending_count = 7, pending_sender = 'Ada'")
conn.commit()
conn.close()
add_sub("own", mode="every5m", member="_op_g_alice_x")
check("upsert: re-choosing the same mode keeps the held count", sub_row("own")["pending_count"] == 7)
add_sub("own", mode="all", member="_op_g_alice_x")
check("upsert: changing mode resets the held count",
      sub_row("own")["pending_count"] == 0 and sub_row("own")["pending_sender"] == "")

# 10. Channel deletion takes subscriptions with it.
r2 = json.loads(srv.nth_connect(summary="t", name="Bea", channel="doomed"))
DOOMED = r2["channel"]
add_sub("doomed", channel=DOOMED)
conn = sqlite3.connect(str(srv.DB_PATH))
check("channel delete: the prune helper removes its subscriptions",
      npush.delete_channel_subscriptions(conn, DOOMED) == 1)
conn.commit()
conn.close()
add_sub("doomed", channel=DOOMED)
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("DELETE FROM channels WHERE code = ?", (DOOMED,))
conn.commit()
conn.close()
fresh(Scripted()).tick()
check("channel delete: a channel removed by any path is swept on the next tick",
      sub_row("doomed", channel=DOOMED) is None)

# 2. Repeated 4xx drops a subscription; a success in between resets the count.
clear_subs()
add_sub("rej")
sc = Scripted()
d = fresh(sc)
sc.status[f"{EP}rej"] = 400
say("one")
d.tick()
say("two")
d.tick()
check("4xx: two refusals are counted", sub_row("rej")["fail_count"] == 2)
sc.status[f"{EP}rej"] = 201
say("three")
d.tick()
check("4xx: a delivery resets the count", sub_row("rej")["fail_count"] == 0)
sc.status[f"{EP}rej"] = 403
for text in ("four", "five", "six"):
    say(text)
    d.tick()
check("4xx: three refusals in a row drop the subscription", sub_row("rej") is None)

# 2. Known-bad endpoints are skipped; sends run in parallel; ticks are capped.
clear_subs()
add_sub("slow-a")
add_sub("bad")
sc = Scripted()
d = fresh(sc)
sc.status[f"{EP}bad"] = 503
say("first")
d.tick()
sc.calls.clear()
say("second")
d.tick()
check("bounds: an endpoint in backoff costs no request",
      [e for e, _p, _u in sc.calls] == [f"{EP}slow-a"])
clear_subs()
for i in range(8):
    add_sub(f"par{i}", member=f"_op_g_par{i}_x")
sc = Scripted()
sc.delay = 0.3
d = fresh(sc)
say("fan out")
t0 = time.monotonic()
d.tick()
elapsed = time.monotonic() - t0
check(f"bounds: 8 slow sends run in parallel ({elapsed:.2f}s, serial would be 2.4s)",
      len(sc.calls) == 8 and elapsed < 1.2)
clear_subs()
add_sub("cap")
_cap = npush.MAX_SENDS_PER_TICK
npush.MAX_SENDS_PER_TICK = 2
try:
    sc = Scripted()
    d = fresh(sc)
    for i in range(5):
        say(f"burst {i}")
    counts = []
    for _ in range(4):
        before = len(sc.calls)
        d.tick()
        counts.append(len(sc.calls) - before)
    check(f"bounds: a tick takes at most the cap and the rest follow ({counts})",
          counts[:3] == [2, 2, 1] and sum(counts) == 5)
finally:
    npush.MAX_SENDS_PER_TICK = _cap

# 3. Undelivered summaries and bangs are kept.
clear_subs()
add_sub("dig", mode="every5m")
sc = Scripted()
d = fresh(sc)
clock[0] += 10_000
sc.status[f"{EP}dig"] = 503
say("lost summary?")
d.tick()
check("retry: a failed summary hands its count back",
      sub_row("dig")["pending_count"] == 1)
say("held while backed off")
d.tick()
check("retry: messages keep counting while the endpoint rests",
      sub_row("dig")["pending_count"] == 2 and len(sc.calls) == 1)
sc.status[f"{EP}dig"] = 201
d._endpoint_backoff.clear()
d.tick()
check("retry: the summary goes out once the endpoint recovers, with every message",
      len(sc.calls) == 2 and sc.calls[-1][1]["title"].endswith("2 new messages")
      and sub_row("dig")["pending_count"] == 0)

clear_subs()
add_sub("bang", mode="mentions")
sc = Scripted()
d = fresh(sc)
sc.status[f"{EP}bang"] = 503
say("!all the build is on fire")
d.tick()
sc.status[f"{EP}bang"] = 201
d._endpoint_backoff.clear()
d.tick()
check("retry: a bang that failed transiently is delivered on a later tick",
      len(sc.calls) == 2 and sc.calls[-1][2] == "high")
sc.status[f"{EP}bang"] = 503
say("!all again")
attempts = 0
for _ in range(10):
    d._endpoint_backoff.clear()
    before = len(sc.calls)
    d.tick()
    attempts += len(sc.calls) - before
check(f"retry: bang retries are bounded ({attempts} sends)",
      attempts == 1 + npush.BANG_RETRY_ATTEMPTS)

# 8. A row that blows up is skipped, never pinning the high-water mark.
clear_subs()
add_sub("iso")
sc = Scripted()
d = fresh(sc)
_decide = npush.decide


def poisoned(mode, msg, *a, **k):
    if msg.get("content") == "poison":
        raise TypeError("malformed row")
    return _decide(mode, msg, *a, **k)


npush.decide = poisoned
try:
    say("poison")
    say("after the poison")
    d.tick()
finally:
    npush.decide = _decide
check("isolation: the message after a malformed one is still delivered",
      [p["body"] for _e, p, _u in sc.calls] == ["after the poison"])
say("later still")
d.tick()
check("isolation: the high-water mark moved past the bad row",
      [p["body"] for _e, p, _u in sc.calls][-1] == "later still" and len(sc.calls) == 2)
clear_subs()

# 9. The whole push body stays within 4096 octets.
check("size: MAX_PLAINTEXT is 3993", npush.MAX_PLAINTEXT == 3993)
huge = npush.encode_payload({"title": "😀" * 3000, "body": "é" * 9000, "tag": "nth-x", "url": "/"})
check("size: an oversized payload is shortened to fit", len(huge) <= npush.MAX_PLAINTEXT)
fits = npush.encrypt_aes128gcm(b"x" * 3993, b64d(UA_PUBLIC), b64d(UA_AUTH))
check("size: a 3993-octet plaintext makes a 4096-octet body", len(fits) == 4096)
try:
    npush.encrypt_aes128gcm(b"x" * 3994, b64d(UA_PUBLIC), b64d(UA_AUTH))
    check("size: 3994 octets are refused", False)
except ValueError:
    check("size: 3994 octets are refused", True)

# 5. VAPID aud is canonical.
for raw in ("https://FCM.GoogleAPIs.com/fcm/send/x", "https://fcm.googleapis.com:443/fcm/send/x",
            "https://fcm.googleapis.com./fcm/send/x"):
    hdr = keys.authorization(raw, "mailto:admin@example.com", now=now)
    aud = npush.verify_jwt(hdr[len("vapid t="):hdr.index(", k=")], keys.public_bytes)["aud"]
    check(f"vapid: aud is canonical for {raw.split('/')[2]}", aud == "https://fcm.googleapis.com")

# ───────── Second review: caps, locks, commit order, aging, races ─────────

# Cap per subscription inside one message row; no duplicates across ticks.
clear_subs()
for i in range(5):
    add_sub(f"row{i}", member=f"_op_g_row{i}_x")
_cap = npush.MAX_SENDS_PER_TICK
npush.MAX_SENDS_PER_TICK = 2
try:
    sc = Scripted()
    d = fresh(sc)
    say("one message, five phones")
    counts = []
    for _ in range(4):
        before = len(sc.calls)
        d.tick()
        counts.append(len(sc.calls) - before)
    eps = [e for e, _p, _u in sc.calls]
    check(f"cap: one row is split across ticks ({counts})", counts[:3] == [2, 2, 1])
    check("cap: every phone gets the message exactly once",
          sorted(eps) == sorted(f"{EP}row{i}" for i in range(5)))
    say("the next one")
    d.tick()
    d.tick()
    d.tick()
    check("cap: the following message still reaches everyone once",
          len([p for _e, p, _u in sc.calls if p["body"] == "the next one"]) == 5)

    # Digests are capped too, and the rest stay pending.
    clear_subs()
    for i in range(3):
        add_sub(f"dg{i}", mode="every5m", member=f"_op_g_dg{i}_x")
    conn = sqlite3.connect(str(srv.DB_PATH))
    conn.execute("UPDATE push_subscriptions SET pending_count = 4, pending_sender = 'Ada', "
                 "last_sent_at = 0")
    conn.commit()
    conn.close()
    sc = Scripted()
    d = fresh(sc)
    check("cap: due summaries beyond the cap wait for the next tick", len(sc.calls) == 2)
    pending = [sub_row(f"dg{i}")["pending_count"] for i in range(3)]
    check(f"cap: the waiting summary keeps its count ({pending})", sorted(pending) == [0, 0, 4])
    d.tick()
    check("cap: and goes out next tick", len(sc.calls) == 3)
finally:
    npush.MAX_SENDS_PER_TICK = _cap

# Planning holds no write lock (only the final batch write does).
clear_subs()
add_sub("lock")
sc = Scripted()
d = fresh(sc)
lock_seen = []


def slow_decide(*a, **k):
    probe = sqlite3.connect(str(srv.DB_PATH), timeout=0.1)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    except sqlite3.Error as exc:
        lock_seen.append(repr(exc))
    finally:
        probe.close()
    return _decide(*a, **k)


npush.decide = slow_decide
try:
    say("planning")
    d._next_sweep = 0.0          # make the orphan sweep run in this tick too
    d.tick()
finally:
    npush.decide = _decide
check("locks: a writer gets in while the dispatcher plans", not lock_seen, "; ".join(lock_seen))

# high_water moves only after the state write commits.
clear_subs()
add_sub("commit", mode="all")
sc = Scripted()
d = fresh(sc)
d.tick()
hw_before = d.high_water
say("written while the DB is locked")
blocker = sqlite3.connect(str(srv.DB_PATH), timeout=5)
blocker.execute("BEGIN IMMEDIATE")
try:
    d.tick()
    raised = False
except sqlite3.OperationalError:
    raised = True
blocker.rollback()
blocker.close()
check("commit: a failed state write surfaces as an error", raised)
check("commit: high_water stays put when the write fails", d.high_water == hw_before)
check("commit: nothing was sent for the unrecorded plan", not sc.calls)
d.tick()
check("commit: the message is delivered once the DB frees up",
      [p["body"] for _e, p, _u in sc.calls] == ["written while the DB is locked"])

clear_subs()
add_sub("rq", mode="mentions")
sc = Scripted()
d = fresh(sc)
sc.status[f"{EP}rq"] = 503
say("!all retry me")
d.tick()
d._endpoint_backoff.clear()
queued = len(d._retry)
say("a second message to force a state write")
blocker = sqlite3.connect(str(srv.DB_PATH), timeout=5)
blocker.execute("BEGIN IMMEDIATE")
try:
    d.tick()
except sqlite3.OperationalError:
    pass
blocker.rollback()
blocker.close()
check("commit: retries taken for a failed plan go back on the queue",
      queued == 1 and len(d._retry) == 1)

# Guest rows that never renew or deliver age out; trusted rows stay.
clear_subs()
add_sub("old-guest")
add_sub("fresh-guest", member="_op_g_fresh_x")
add_sub("old-owner", member="_op_t_owner_x", tier=npush.TIER_TRUSTED)
_clock_before_aging = clock[0]
clock[0] = 2_000_000_000.0     # a realistic epoch, so "a month ago" is positive
long_ago = clock[0] - npush.GUEST_IDLE_S - 10
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("UPDATE push_subscriptions SET updated_at = ?, last_ok_at = 0", (long_ago,))
conn.execute("UPDATE push_subscriptions SET last_ok_at = ? WHERE endpoint LIKE '%fresh-guest'",
             (clock[0],))
conn.commit()
conn.close()
fresh(Scripted())
check("aging: an idle guest row is removed", sub_row("old-guest") is None)
check("aging: a guest row that still receives pushes stays", sub_row("fresh-guest") is not None)
check("aging: an idle trusted row stays", sub_row("old-owner") is not None)
clock[0] = _clock_before_aging

# The quota check and insert are atomic.
clear_subs()
npush.TIER_QUOTAS[npush.TIER_GUEST] = (8, 3)
results = []
barrier = threading.Barrier(10)


def racer(i):
    conn = sqlite3.connect(str(srv.DB_PATH), timeout=10)
    barrier.wait()
    try:
        npush.upsert_subscription(conn, channel=CH, endpoint=f"{EP}race{i}", p256dh=UA_PUBLIC,
                                  auth=UA_AUTH, member_id=f"_op_g_race{i}_x",
                                  member_name="r", mode="all")
        results.append("ok")
    except npush.SubscriptionLimit:
        results.append("limit")
    finally:
        conn.close()


try:
    threads = [threading.Thread(target=racer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
finally:
    npush.TIER_QUOTAS.update(_quotas)
conn = sqlite3.connect(str(srv.DB_PATH))
n_rows = conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
conn.close()
check(f"race: ten racers for three guest slots leave exactly three rows ({n_rows})",
      n_rows == 3 and results.count("ok") == 3)

# The dispatcher advertises itself for the status endpoint.
clear_subs()
fresh(Scripted(), holder="hub-a:1:x", version="v-test")
conn = sqlite3.connect(str(srv.DB_PATH))
marker = npush.dispatcher_marker(conn)
conn.execute("DELETE FROM push_dispatcher")
conn.commit()
conn.close()
check("marker: the dispatcher records its holder and version",
      marker is not None and marker[:2] == ("hub-a:1:x", "v-test"))
clear_subs()

# ───────── Third review: migration, move, outcome writes, races ─────────
OLD_SCHEMA = (
    "CREATE TABLE push_subscriptions ("
    " channel TEXT NOT NULL, endpoint TEXT NOT NULL, member_id TEXT NOT NULL,"
    " member_name TEXT NOT NULL DEFAULT '', p256dh TEXT NOT NULL, auth TEXT NOT NULL,"
    " mode TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
    " last_sent_at REAL NOT NULL DEFAULT 0, pending_count INTEGER NOT NULL DEFAULT 0,"
    " pending_sender TEXT NOT NULL DEFAULT '', PRIMARY KEY (channel, endpoint))")


def old_db(path):
    """A hub DB as build 200b513 left it: no tier, fail_count or last_ok_at."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE channels (code TEXT)")
    conn.execute("INSERT INTO channels VALUES ('c')")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, channel TEXT, member_id TEXT, "
                 "member_name TEXT, content TEXT, mentions TEXT, bangs TEXT, recipients TEXT, "
                 "retracted_at TEXT)")
    conn.execute(OLD_SCHEMA)
    return conn


mig_dir = Path(tempfile.mkdtemp(prefix="nth_webpush_mig_"))
mig_db = mig_dir / "nth.db"
t0 = 2_000_000_000.0
conn = old_db(mig_db)
for ep, member, mode in (("owner-a", "_op_t_owner", "mentions"),
                         ("owner-b", "_op_t_owner", "mentions"),
                         ("guest-old", "_op_g_old_x", "mentions"),
                         ("old-off", "_op_t_owner", "off")):
    conn.execute("INSERT INTO push_subscriptions (channel, endpoint, member_id, p256dh, auth, "
                 "mode, created_at, updated_at) VALUES ('c', ?, ?, ?, ?, ?, ?, ?)",
                 (f"{EP}{ep}", member, UA_PUBLIC, UA_AUTH, mode, t0, t0))
conn.commit()
npush.ensure_push_table(conn)
conn.commit()
tiers = dict(conn.execute("SELECT endpoint, tier FROM push_subscriptions").fetchall())
check("migrate: rows from before the tier column become 'legacy'",
      set(tiers.values()) == {npush.TIER_LEGACY})
check("migrate: rows from before show_text hide message text (0)",
      {r[0] for r in conn.execute("SELECT show_text FROM push_subscriptions")} == {0})
npush.upsert_subscription(conn, channel="c", endpoint=f"{EP}owner-a", p256dh=UA_PUBLIC,
                          auth=UA_AUTH, member_id="_op_t_owner", member_name="Owner",
                          mode="mentions", tier=npush.TIER_TRUSTED, now=t0 + 86400)
npush.upsert_subscription(conn, channel="c", endpoint=f"{EP}guest-old", p256dh=UA_PUBLIC,
                          auth=UA_AUTH, member_id="_op_g_old_x", member_name="old-guest",
                          mode="mentions", tier=npush.TIER_GUEST, now=t0 + 86400)
npush.upsert_subscription(conn, channel="c", endpoint=f"{EP}guest-new", p256dh=UA_PUBLIC,
                          auth=UA_AUTH, member_id="_op_g_new_x", member_name="new-guest",
                          mode="mentions", tier=npush.TIER_GUEST, now=t0 + 31 * 86400)
tiers = dict(conn.execute("SELECT endpoint, tier FROM push_subscriptions").fetchall())
check("migrate: renewing re-tiers a legacy row from the caller's identity",
      tiers[f"{EP}owner-a"] == npush.TIER_TRUSTED and tiers[f"{EP}guest-old"] == npush.TIER_GUEST)
conn.close()
d = npush.PushDispatcher(mig_db, mig_dir, sender=Scripted(), clock=lambda: t0 + 32 * 86400)
d._housekeeping()
conn = sqlite3.connect(str(mig_db))
left = {r[0] for r in conn.execute("SELECT endpoint FROM push_subscriptions")}
check("migrate: after 32 days the re-tiered owner row survives the sweep", f"{EP}owner-a" in left)
check("migrate: a legacy row that was never renewed is not aged out", f"{EP}owner-b" in left)
check("migrate: a genuinely stale guest row still ages out", f"{EP}guest-old" not in left)
check("migrate: a recently renewed guest row stays", f"{EP}guest-new" in left)
check("migrate: an old build's mode-off row is swept", f"{EP}old-off" not in left)
_q = dict(npush.TIER_QUOTAS)
npush.TIER_QUOTAS[npush.TIER_TRUSTED] = (64, 2)
try:
    try:
        npush.upsert_subscription(conn, channel="c", endpoint=f"{EP}owner-c", p256dh=UA_PUBLIC,
                                  auth=UA_AUTH, member_id="_op_t_other", member_name="o",
                                  mode="all", tier=npush.TIER_TRUSTED)
        check("migrate: legacy rows count against the trusted pool", False)
    except npush.SubscriptionLimit:
        check("migrate: legacy rows count against the trusted pool", True)
finally:
    npush.TIER_QUOTAS.update(_q)
conn.close()


class StaleTableInfo(sqlite3.Connection):
    """Reports the pre-migration columns, as a racing second process would
    have seen them a moment before the first one added them."""

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA table_info(push_subscriptions)"):
            return super().execute("SELECT 0, 'channel'")
        return super().execute(sql, *args)


race_dir = Path(tempfile.mkdtemp(prefix="nth_webpush_race_"))
old_db(race_dir / "nth.db").close()
first = sqlite3.connect(str(race_dir / "nth.db"))
npush.ensure_push_table(first)
first.commit()
first.close()
loser = sqlite3.connect(str(race_dir / "nth.db"), factory=StaleTableInfo)
try:
    npush.ensure_push_table(loser)
    check("migrate: a duplicate-column race on first start is treated as success", True)
except sqlite3.OperationalError as exc:
    check("migrate: a duplicate-column race on first start is treated as success", False, str(exc))
loser.close()

# Moving a guest at its per-member quota to a new endpoint keeps every channel.
move_dir = Path(tempfile.mkdtemp(prefix="nth_webpush_move_"))
mconn = sqlite3.connect(str(move_dir / "n.db"))
E1, E2 = f"{EP}E1", f"{EP}E2"
_q = dict(npush.TIER_QUOTAS)
npush.TIER_QUOTAS[npush.TIER_GUEST] = (5, 200)
try:
    npush.upsert_subscription(mconn, channel="c5", endpoint=E1, p256dh=UA_PUBLIC, auth=UA_AUTH,
                              member_id="X", member_name="x", mode="all")
    for ch in ("c1", "c2", "c3", "c4"):
        npush.upsert_subscription(mconn, channel=ch, endpoint=E1, p256dh=UA_PUBLIC,
                                  auth=UA_AUTH, member_id="Y", member_name="y", mode="mentions")
    npush.upsert_subscription(mconn, channel="c5", endpoint=E2, p256dh=UA_PUBLIC, auth=UA_AUTH,
                              member_id="Y", member_name="y", mode="all")
    moved = npush.move_endpoint(mconn, member_id="Y", old_endpoint=E1, new_endpoint=E2,
                                p256dh=UA_PUBLIC, auth=UA_AUTH)
finally:
    npush.TIER_QUOTAS.update(_q)
rows = mconn.execute("SELECT channel, endpoint, member_id, mode FROM push_subscriptions "
                     "ORDER BY channel").fetchall()
check(f"move: a member at its quota keeps every channel ({moved})",
      sorted(moved) == ["c1", "c2", "c3", "c4"]
      and [r for r in rows if r[2] == "Y"] == [(c, E2, "Y", "mentions") for c in ("c1", "c2", "c3", "c4")]
      + [("c5", E2, "Y", "all")])
check("move: another identity's row on the old endpoint is untouched",
      ("c5", E1, "X", "all") in rows)
E3, E4 = f"{EP}E3", f"{EP}E4"
npush.upsert_subscription(mconn, channel="cz", endpoint=E3, p256dh=UA_PUBLIC, auth=UA_AUTH,
                          member_id="Z", member_name="z", mode="all", show_text=True)
mconn.execute("UPDATE push_subscriptions SET last_ok_at = 777 WHERE endpoint = ?", (E3,))
npush.move_endpoint(mconn, member_id="Z", old_endpoint=E3, new_endpoint=E4,
                    p256dh=UA_PUBLIC, auth=UA_AUTH)
check("move: the text choice travels to the new endpoint, the last delivery does not",
      mconn.execute("SELECT show_text, last_ok_at FROM push_subscriptions WHERE endpoint = ?",
                    (E4,)).fetchone() == (1, 0.0))
mconn.close()

# ───────── Device controls: show_text, last delivery ─────────
# A DB from the build just before show_text: every other column present.
PREV_SCHEMA = OLD_SCHEMA.replace(
    " PRIMARY KEY (channel, endpoint))",
    " tier TEXT NOT NULL DEFAULT 'legacy', fail_count INTEGER NOT NULL DEFAULT 0,"
    " last_ok_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (channel, endpoint))")
prev_dir = Path(tempfile.mkdtemp(prefix="nth_webpush_prev_"))
pconn = sqlite3.connect(str(prev_dir / "n.db"))
pconn.execute(PREV_SCHEMA)
pconn.execute("INSERT INTO push_subscriptions (channel, endpoint, member_id, p256dh, auth, mode, "
              "created_at, updated_at, tier, last_ok_at) VALUES ('c', ?, 'M', ?, ?, 'all', 1, 1, "
              "'trusted', 5)", (f"{EP}prev", UA_PUBLIC, UA_AUTH))
pconn.commit()
npush.ensure_push_table(pconn)
pconn.commit()
check("migrate: an existing subscription gains show_text = 0 (hidden)",
      pconn.execute("SELECT show_text, last_ok_at FROM push_subscriptions").fetchone() == (0, 5.0))


def shown(conn, ep, channel="c"):
    return conn.execute("SELECT show_text FROM push_subscriptions WHERE channel = ? AND "
                        "endpoint = ?", (channel, ep)).fetchone()[0]


def up(conn, ep, member="M", **kw):
    npush.upsert_subscription(conn, channel="c", endpoint=ep, p256dh=UA_PUBLIC, auth=UA_AUTH,
                              member_id=member, member_name="m", mode=kw.pop("mode", "all"),
                              tier=npush.TIER_TRUSTED, **kw)


up(pconn, f"{EP}new")
check("store: a new subscription hides text unless asked", shown(pconn, f"{EP}new") == 0)
up(pconn, f"{EP}new", show_text=True)
check("store: subscribe with show_text stores it", shown(pconn, f"{EP}new") == 1)
up(pconn, f"{EP}new", mode="mentions")
check("store: a renewal or mode change without show_text keeps the choice",
      shown(pconn, f"{EP}new") == 1)
up(pconn, f"{EP}new", show_text=False)
check("store: subscribe with show_text false turns it back off", shown(pconn, f"{EP}new") == 0)
up(pconn, f"{EP}born-shown", show_text=True)
check("store: a new subscription can start with text shown", shown(pconn, f"{EP}born-shown") == 1)
check("store: set_show_text changes the owner's row",
      npush.set_show_text(pconn, member_id="M", endpoint=f"{EP}new", channel="c",
                          show_text=True) == 1 and shown(pconn, f"{EP}new") == 1)
check("store: set_show_text refuses another identity's row",
      npush.set_show_text(pconn, member_id="X", endpoint=f"{EP}new", channel="c",
                          show_text=False) == 0 and shown(pconn, f"{EP}new") == 1)
check("store: set_show_text on a missing row changes nothing",
      npush.set_show_text(pconn, member_id="M", endpoint=f"{EP}none", channel="c",
                          show_text=True) == 0)
check("store: own_subscription finds only the caller's row",
      npush.own_subscription(pconn, member_id="M", endpoint=f"{EP}new", channel="c")["show_text"]
      is True
      and npush.own_subscription(pconn, member_id="X", endpoint=f"{EP}new", channel="c") is None)
npush.record_test_outcome(pconn, channel="c", endpoint=f"{EP}new", status=201, now=1234.0)
check("store: a successful test marks the row delivered",
      pconn.execute("SELECT last_ok_at, fail_count FROM push_subscriptions WHERE endpoint = ?",
                    (f"{EP}new",)).fetchone() == (1234.0, 0))
pconn.execute("UPDATE push_subscriptions SET fail_count = 2 WHERE endpoint = ?", (f"{EP}new",))
npush.record_test_outcome(pconn, channel="c", endpoint=f"{EP}new", status=403, now=9999.0)
check("store: a refused test leaves the delivery record and rejection count alone",
      pconn.execute("SELECT last_ok_at, fail_count FROM push_subscriptions WHERE endpoint = ?",
                    (f"{EP}new",)).fetchone() == (1234.0, 2))
npush.record_test_outcome(pconn, channel="c", endpoint=f"{EP}new", status=410)
check("store: a test answered 410 forgets the endpoint",
      npush.own_subscription(pconn, member_id="M", endpoint=f"{EP}new", channel="c") is None)
pconn.close()

# The dispatcher sends each row's choice.
clear_subs()
add_sub("hide", show_text=False)
add_sub("show", show_text=True)
sc = Scripted()
d = fresh(sc)
say("rotate the staging keys tonight")
d.tick()
bodies = {e[len(EP):]: p["body"] for e, p, _u in sc.calls}
check("dispatch: a device that has not opted in gets the neutral line",
      bodies.get("hide") == npush.HIDDEN_BODY, str(bodies))
check("dispatch: a device that opted in gets the message text",
      bodies.get("show") == "rotate the staging keys tonight", str(bodies))
check("dispatch: a delivery records last_ok_at", sub_row("hide")["last_ok_at"] == clock[0])
clear_subs()

# A failed outcome write is kept and applied on the next tick.
clear_subs()
add_sub("rec", mode="every5m")
sc = Scripted()
d = fresh(sc)
real_connect = d._connect


def impatient_connect():
    conn = real_connect()
    conn.execute("PRAGMA busy_timeout=50")
    return conn


d._connect = impatient_connect
clock[0] += 10_000
say("summary that will fail")
locker = []


def failing_then_locking(endpoint, *a, **k):
    # Opened on the sender's worker thread and closed from the test, so the
    # connection must not be pinned to its creating thread.
    hold = sqlite3.connect(str(srv.DB_PATH), timeout=5, check_same_thread=False)
    hold.execute("BEGIN IMMEDIATE")
    locker.append(hold)
    return 503


d._send = failing_then_locking
d.tick()
check("record: an outcome write that cannot commit is kept for later",
      len(d._record_backlog) == 1 and sub_row("rec")["pending_count"] == 0)
for h in locker:
    h.rollback()
    h.close()
d._send = sc
d.tick()
check("record: the kept write lands on the next tick (the summary count is back)",
      not d._record_backlog and sub_row("rec")["pending_count"] >= 1)

# A mode change during planning is not overwritten by the plan's write.
clear_subs()
add_sub("mc", mode="every5m", member="_op_g_mc_x")
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("UPDATE push_subscriptions SET pending_count = 5, pending_sender = 'Ada', "
             "last_sent_at = ?", (clock[0],))
conn.commit()
conn.close()
sc = Scripted()
d = fresh(sc)
switched = []


def switch_mode_mid_plan(mode, msg, member_id, *a, **k):
    if not switched:
        switched.append(1)
        add_sub("mc", mode="all", member="_op_g_mc_x")      # resets the held count
    return _decide(mode, msg, member_id, *a, **k)


npush.decide = switch_mode_mid_plan
try:
    say("one more while every5m")
    d.tick()
finally:
    npush.decide = _decide
row = sub_row("mc")
check(f"plan: a concurrent mode change keeps its reset (mode {row['mode']}, "
      f"pending {row['pending_count']})", row["mode"] == "all" and row["pending_count"] == 0)

# The advertisement stays fresh through a long send phase.
check("marker: freshness outlasts the worst-case tick",
      npush.MARKER_FRESH_S > npush.MAX_SENDS_PER_TICK / npush.SEND_WORKERS * npush.SEND_TIMEOUT_S)
clear_subs()
for i in range(3):
    add_sub(f"hb{i}", member=f"_op_g_hb{i}_x")
seen = []


def heartbeat_probe(*a, **k):
    time.sleep(0.05)
    conn = sqlite3.connect(str(srv.DB_PATH))
    seen.append(npush.dispatcher_marker(conn)[2])
    conn.close()
    return 201


_mi, _workers = npush.MARKER_INTERVAL_S, npush.SEND_WORKERS
npush.MARKER_INTERVAL_S, npush.SEND_WORKERS = 0.0, 1
try:
    d = fresh(heartbeat_probe, holder="hub-hb:1:x", version="t")
    say("long send phase")
    d.tick()
finally:
    npush.MARKER_INTERVAL_S, npush.SEND_WORKERS = _mi, _workers
check("marker: the heartbeat advances during the send phase",
      len(seen) == 3 and seen[-1] > seen[0])
conn = sqlite3.connect(str(srv.DB_PATH))
conn.execute("DELETE FROM push_dispatcher")
conn.commit()
conn.close()
clear_subs()

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


def _owner_row_show_text(endpoint):
    conn = sqlite3.connect(str(srv.DB_PATH))
    try:
        return conn.execute("SELECT show_text FROM push_subscriptions WHERE endpoint = ?",
                            (endpoint,)).fetchone()[0]
    finally:
        conn.close()


def srow(endpoint, mode, show_text=False, last_ok_at=None):
    """One entry of /api/push/status `subscriptions`."""
    return {"endpoint": endpoint, "mode": mode, "show_text": show_text, "last_ok_at": last_ok_at}


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

        npush.TIER_QUOTAS[npush.TIER_GUEST] = (8, 0)
        try:
            st, _hd, _raw = call(port, "POST", "/api/push/subscribe",
                                 {**SUB, "subscription": {**GOOD_SUB, "endpoint": GOOD_SUB["endpoint"] + "full"}})
            check("subscribe: a full guest pool answers 429", st == 429)
            owner = web.OperatorIdentity(member_id="_op_t_owner2", name="owner",
                                         source=web.IDENTITY_SOURCE_TAILSCALE)
            web.NthWebHandler._resolve_identity = lambda self, _i=owner: (None, _i, False)
            st, _hd, _raw = call(port, "POST", "/api/push/subscribe",
                                 {**SUB, "subscription": {**GOOD_SUB, "endpoint": GOOD_SUB["endpoint"] + "own"}})
            check("subscribe: the owner still subscribes while the guest pool is full", st == 200)
            web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)
        finally:
            npush.TIER_QUOTAS.update(_quotas)
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe", {**SUB, "mode": "every5m"})
        check("subscribe: named guest can subscribe", st == 200)
        NEW_EP = GOOD_SUB["endpoint"] + "-moved"
        st, _hd, raw = call(port, "POST", "/api/push/move",
                            {"old_endpoint": GOOD_SUB["endpoint"],
                             "subscription": {**GOOD_SUB, "endpoint": NEW_EP}})
        check("move: the page's move call re-points this identity's rows",
              st == 200 and as_json(raw).get("moved") == [CH])
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("move: status shows the new endpoint",
              as_json(raw).get("subscriptions") == [srow(NEW_EP, "every5m")])
        st, _hd, _raw = call(port, "POST", "/api/push/move",
                             {"old_endpoint": NEW_EP,
                              "subscription": {**GOOD_SUB, "endpoint": "https://127.0.0.1/x"}})
        check("move: a non-push-service endpoint is refused (400)", st == 400)
        st, _hd, raw = call(port, "POST", "/api/push/move",
                            {"old_endpoint": NEW_EP, "subscription": GOOD_SUB})
        check("move: and back again", st == 200 and as_json(raw).get("moved") == [CH])
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        status = as_json(raw)
        check("status: shows this identity's mode for the channel",
              st == 200 and status.get("subscriptions") == [
                  srow(GOOD_SUB["endpoint"], "every5m")], str(status))
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe", {**SUB, "mode": "all"})
        st, _hd, raw_nd = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: a server with no sending hub says it is not delivering",
              st == 200 and as_json(raw_nd).get("delivering") is False)
        _c = sqlite3.connect(str(srv.DB_PATH))
        web.AgentControlLease(srv.DB_PATH)._db().close()      # create the table
        _c.execute("INSERT OR REPLACE INTO agent_control_lease (id, holder, host, pid, "
                   "acquired_at, expires_at) VALUES (1, 'hub-a:1:x', 'hub-a', 1, 'now', ?)",
                   (time.time() + 60,))
        _c.execute("DELETE FROM push_dispatcher")
        _c.commit()

        def delivering():
            _st, _h, body = call(port, "GET", f"/api/push/status?channel={CH}")
            return as_json(body).get("delivering")
        check("status: a lease holder that never advertised push is not delivering",
              delivering() is False)
        _c.execute("INSERT INTO push_dispatcher (id, holder, version, heartbeat_at) "
                   "VALUES (1, 'hub-a:1:x', 'test', ?)", (time.time(),))
        _c.commit()
        check("status: the lease holder advertising a dispatcher is delivering",
              delivering() is True)
        _c.execute("UPDATE push_dispatcher SET heartbeat_at = ?",
                   (time.time() - npush.MARKER_FRESH_S - 5,))
        _c.commit()
        check("status: a stale dispatcher advertisement does not count", delivering() is False)
        _c.execute("UPDATE push_dispatcher SET holder = 'hub-b:2:y', heartbeat_at = ?",
                   (time.time(),))
        _c.commit()
        check("status: an advertisement from a hub that lost the lease does not count",
              delivering() is False)
        _c.execute("DELETE FROM agent_control_lease")
        _c.execute("DELETE FROM push_dispatcher")
        _c.commit()
        _c.close()
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe", {**SUB, "mode": "off"})
        check("subscribe: mode off is refused over HTTP (400)", st == 400)
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: changing the mode replaces, never duplicates",
              as_json(raw).get("subscriptions") == [srow(GOOD_SUB["endpoint"], "all")])

        thief = web.OperatorIdentity(member_id="_op_g_thief_x", name="thief", source=web.IDENTITY_SOURCE_GUEST)
        web.NthWebHandler._resolve_identity = lambda self, _i=thief: (None, _i, False)
        st, _hd, raw = call(port, "POST", "/api/push/unsubscribe",
                            {"endpoint": GOOD_SUB["endpoint"], "channel": CH})
        check("unsubscribe: another identity cannot remove it", as_json(raw).get("removed") == 0)
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe", SUB)
        check("subscribe: another identity presenting the same endpoint gets 409", st == 409)
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: another identity does not see it", as_json(raw).get("subscriptions") == [])

        web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)
        st, _hd, raw = call(port, "POST", "/api/push/unsubscribe",
                            {"endpoint": GOOD_SUB["endpoint"], "channel": CH})
        check("unsubscribe: the owner removes it", st == 200 and as_json(raw).get("removed") == 1)
        st, _hd, raw = call(port, "GET", f"/api/push/status?channel={CH}")
        check("status: empty after unsubscribe", as_json(raw).get("subscriptions") == [])

        # ── Device controls over HTTP: show_text, last delivery, Send test ──
        DEV = {**GOOD_SUB, "endpoint": GOOD_SUB["endpoint"] + "-dev"}
        DEV_EP = DEV["endpoint"]
        TARGET = {"endpoint": DEV_EP, "channel": CH}

        def my_row():
            body = as_json(call(port, "GET", f"/api/push/status?channel={CH}")[2])
            return next((r for r in body.get("subscriptions", []) if r["endpoint"] == DEV_EP), None)

        st, _hd, _raw = call(port, "POST", "/api/push/subscribe",
                             {"subscription": DEV, "channel": CH, "mode": "all", "show_text": "yes"})
        check("subscribe: a non-boolean show_text is refused (400)", st == 400)
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe",
                             {"subscription": DEV, "channel": CH, "mode": "all"})
        row = my_row()
        check("status: a new device hides text and has never been delivered to",
              st == 200 and row == srow(DEV_EP, "all"), str(row))
        st, _hd, _raw = call(port, "POST", "/api/push/subscribe",
                             {"subscription": DEV, "channel": CH, "mode": "all", "show_text": True})
        check("subscribe: show_text true is stored", my_row()["show_text"] is True)
        st, _hd, raw = call(port, "POST", "/api/push/subscribe",
                            {"subscription": DEV, "channel": CH, "mode": "mentions"})
        check("subscribe: the page's quiet renewal (no show_text) keeps the choice",
              my_row() == srow(DEV_EP, "mentions", show_text=True))
        check("subscribe: the reply reports the stored choice", as_json(raw).get("show_text") is True)
        st, _hd, raw = call(port, "POST", "/api/push/settings", {**TARGET, "show_text": False})
        check("settings: the owner turns message text off without re-subscribing",
              st == 200 and as_json(raw).get("show_text") is False
              and my_row()["show_text"] is False)
        st, _hd, _raw = call(port, "POST", "/api/push/settings", {**TARGET, "show_text": 1})
        check("settings: a non-boolean show_text is refused (400)", st == 400)
        st, _hd, _raw = call(port, "POST", "/api/push/settings",
                             {"endpoint": DEV_EP + "-nope", "channel": CH, "show_text": True})
        check("settings: a missing subscription answers 404", st == 404)
        st, _hd, _raw = call(port, "POST", "/api/push/settings", {**TARGET, "show_text": True},
                             headers={"Origin": "https://evil.example.com"})
        check("settings: cross-origin POST refused", st == 403)

        sends = Scripted()
        _real_send, _real_limiter = npush.send_push, npush.TEST_LIMITER
        test_clock = [1000.0]
        npush.send_push = sends
        npush.TEST_LIMITER = npush.TestPushLimiter(clock=lambda: test_clock[0])
        try:
            web.NthWebHandler._resolve_identity = lambda self, _i=thief: (None, _i, False)
            st, _hd, _raw = call(port, "POST", "/api/push/settings", {**TARGET, "show_text": True})
            check("settings: another identity cannot change it (404, unchanged)",
                  st == 404 and _owner_row_show_text(DEV_EP) == 0)
            st, _hd, _raw = call(port, "POST", "/api/push/test", TARGET)
            check("test: another identity cannot push to this device (404, nothing sent)",
                  st == 404 and not sends.calls)
            web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)

            st, _hd, _raw = call(port, "POST", "/api/push/test",
                                 {"endpoint": DEV_EP + "-nope", "channel": CH})
            check("test: a device with no subscription here answers 404, nothing sent",
                  st == 404 and not sends.calls)
            st, _hd, _raw = call(port, "POST", "/api/push/test", TARGET,
                                 headers={"Origin": "https://evil.example.com"})
            check("test: cross-origin POST refused", st == 403 and not sends.calls)

            before = time.time()
            st, _hd, raw = call(port, "POST", "/api/push/test", TARGET)
            check("test: the owner's own device gets exactly one push",
                  st == 200 and [c[0] for c in sends.calls] == [DEV_EP], f"{st} {raw[:120]!r}")
            check("test: the push is the synthetic test notification",
                  sends.calls and sends.calls[0][1] == npush.test_payload(CH))
            row = my_row()
            check("status: reports last_ok_at after a delivered test",
                  row["last_ok_at"] is not None and row["last_ok_at"] >= before
                  and as_json(raw).get("last_ok_at") == row["last_ok_at"], str(row))
            st, _hd, raw = call(port, "POST", "/api/push/test", TARGET)
            check("test: a second press within 10 s is rate-limited (429), nothing sent",
                  st == 429 and len(sends.calls) == 1 and b"wait" in raw.lower())
            test_clock[0] += npush.TEST_PUSH_INTERVAL_S
            sends.status[DEV_EP] = 503
            st, _hd, raw = call(port, "POST", "/api/push/test", TARGET)
            check("test: a push-service failure says so (502) and keeps the last delivery",
                  st == 502 and b"503" in raw and my_row()["last_ok_at"] == row["last_ok_at"])
            test_clock[0] += npush.TEST_PUSH_INTERVAL_S
            sends.status[DEV_EP] = 410
            st, _hd, raw = call(port, "POST", "/api/push/test", TARGET)
            check("test: an expired subscription answers 410 and is forgotten",
                  st == 410 and my_row() is None)

            # A row an older build stored for a host the allowlist now refuses.
            LEGACY_EP = "https://push.example.com/old/device"
            _c = sqlite3.connect(str(srv.DB_PATH))
            _c.execute("INSERT INTO push_subscriptions (channel, endpoint, member_id, p256dh, auth, "
                       "mode, created_at, updated_at, tier) VALUES (?, ?, ?, ?, ?, 'all', 1, 1, 'guest')",
                       (CH, LEGACY_EP, ident.member_id, UA_PUBLIC, UA_AUTH))
            _c.commit()
            _c.close()
            n_before = len(sends.calls)
            test_clock[0] += npush.TEST_PUSH_INTERVAL_S
            st, _hd, raw = call(port, "POST", "/api/push/test", {"endpoint": LEGACY_EP, "channel": CH})
            check("test: an endpoint off the allowlist gets its own message, not 'check your connection'",
                  st == 422 and b"not one the hub sends to" in raw and b"connection" not in raw
                  and len(sends.calls) == n_before, f"{st} {raw[:160]!r}")

            pending_v = web.OperatorIdentity(member_id="_op_p_v", name="", source=web.IDENTITY_SOURCE_PENDING)
            web.NthWebHandler._resolve_identity = lambda self, _i=pending_v: (None, _i, False)
            st_set, _hd, _raw = call(port, "POST", "/api/push/settings", {**TARGET, "show_text": True})
            st_test, _hd, _raw = call(port, "POST", "/api/push/test", TARGET)
            check("settings: a pending visitor is refused (403)", st_set == 403)
            check("test: a pending visitor is refused (403), nothing sent",
                  st_test == 403 and len(sends.calls) == n_before)

            # Guests churning identities and endpoints meet the guest tier's
            # budget; the owner's test still goes out.
            npush.TEST_LIMITER = npush.TestPushLimiter(clock=lambda: test_clock[0])
            burst = npush.TEST_PUSH_TIER_BUDGET[npush.TIER_GUEST][0]
            churn = []
            for i in range(burst + 2):
                g = web.OperatorIdentity(member_id=f"_op_g_ch{i}_x", name=f"ch{i}",
                                         source=web.IDENTITY_SOURCE_GUEST)
                web.NthWebHandler._resolve_identity = lambda self, _i=g: (None, _i, False)
                ep = {**GOOD_SUB, "endpoint": f"{GOOD_SUB['endpoint']}-churn{i}"}
                call(port, "POST", "/api/push/subscribe", {"subscription": ep, "channel": CH, "mode": "all"})
                churn.append(call(port, "POST", "/api/push/test",
                                  {"endpoint": ep["endpoint"], "channel": CH})[0])
            check(f"test: guests churning endpoints are cut off after the tier budget ({churn})",
                  churn[:burst] == [200] * burst and set(churn[burst:]) == {429})
            boss = web.OperatorIdentity(member_id="_op_t_boss", name="boss",
                                        source=web.IDENTITY_SOURCE_TAILSCALE)
            web.NthWebHandler._resolve_identity = lambda self, _i=boss: (None, _i, False)
            BOSS = {**GOOD_SUB, "endpoint": GOOD_SUB["endpoint"] + "-boss"}
            call(port, "POST", "/api/push/subscribe", {"subscription": BOSS, "channel": CH, "mode": "all"})
            st, _hd, raw = call(port, "POST", "/api/push/test",
                                {"endpoint": BOSS["endpoint"], "channel": CH})
            check("test: the trusted tier still sends while the guest tier is exhausted",
                  st == 200, f"{st} {raw[:120]!r}")
        finally:
            npush.send_push, npush.TEST_LIMITER = _real_send, _real_limiter
            web.NthWebHandler._resolve_identity = lambda self, _i=ident: (None, _i, False)

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
