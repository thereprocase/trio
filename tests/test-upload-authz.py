"""Tests for who may POST /api/upload, and how much they may store.

Two independent controls, because they close different doors:

  * the IDENTITY GATE stops a self-declared guest. Under --tailnet (the
    deployed mode) a guest is anyone who can reach the port and type a name,
    and an upload writes into the operator's home directory -- the same class
    of action /api/reveal is already gated for.

  * the PER-MEMBER QUOTA stops a flood from an identity that IS allowed. It is
    not redundant with the gate: a cross-site POST executes as the trusted
    loopback operator and therefore passes the gate. It is also not redundant
    with MAX_UPLOAD_BYTES, which bounds one request and says nothing about the
    sum. sweep_attachments only reclaims UNLINKED rows, so bytes linked to a
    message are permanent -- the quota is the only bound on total growth.

Delete either control and one of these tests goes red.

Usage: python tests/test-upload-authz.py
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []
skips = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_upload_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"
web.ATTACH_DIR = Path(_tmp) / "attachments"

# Smallest byte string sniff_image_mime() accepts as a PNG, padded to a known
# size. The sniffer reads the 8-byte signature only, so the padding is inert
# and the test does not need a real encoder.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4088          # 4096 bytes exactly


def upload(port, payload, filename="x.png"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/upload", data=payload, method="POST")
    req.add_header("Content-Type", "image/png")
    req.add_header("X-Filename", filename)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


r = json.loads(srv.nth_connect(summary="t", name="R", channel="uploadtest"))
CH = r["channel"]

hub = web.EventHub(srv.DB_PATH, CH)
server = None
try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    # ── identity gate ───────────────────────────────────────────────────────
    # The test connects over loopback, which resolves as a trusted operator, so
    # the untrusted tiers have to be injected the same way test-file-reveal.py
    # does it.
    _real = web.NthWebHandler._resolve_identity
    try:
        for source, label in ((web.IDENTITY_SOURCE_GUEST, "guest"),
                              (web.IDENTITY_SOURCE_PENDING, "pending")):
            class _Ident:
                pass
            _Ident.source = source
            _Ident.name = label
            _Ident.summary = label
            web.NthWebHandler._resolve_identity = lambda self: (None, _Ident(), False)
            st, _b = upload(port, PNG)
            check(f"authz: {label} cannot upload (403)", st == 403)
    finally:
        web.NthWebHandler._resolve_identity = _real

    # A trusted operator (this loopback connection) still can -- the gate must
    # not have simply broken uploading for everyone.
    st, body = upload(port, PNG, "first.png")
    check("authz: trusted operator can upload", st == 200 and body.get("ok") is True)

    # ── per-member quota ────────────────────────────────────────────────────
    # 4096 bytes are already stored above; a 6 KB ceiling admits nothing more.
    _real_quota = web.MAX_MEMBER_ATTACH_BYTES
    try:
        web.MAX_MEMBER_ATTACH_BYTES = 6144
        st, body = upload(port, PNG, "second.png")
        check("quota: upload past the per-member ceiling is refused (413)", st == 413)
        check("quota: refusal names the reason",
              "quota" in (body.get("error") or ""))

        # The ceiling is a SUM, not a per-request cap: raise it and the same
        # request succeeds. Without this, a test could pass against a bug that
        # rejects every second upload for any reason at all.
        web.MAX_MEMBER_ATTACH_BYTES = 1024 * 1024
        st, _b = upload(port, PNG, "third.png")
        check("quota: same upload succeeds once the ceiling is raised", st == 200)
    finally:
        web.MAX_MEMBER_ATTACH_BYTES = _real_quota

except OSError as e:
    print(f"SKIP: upload-authz (could not start server: {e})", file=sys.stderr)
    skips.append("upload-authz")
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    hub.stop()

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), {len(skips)} skip(s)")
sys.exit(1 if failures else 0)
