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
import nth_media as nmedia  # noqa: E402

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

    # A member the hub owner listed is a Tailscale-proven person: they may upload,
    # and still hold no local-path power. A self-declared guest may not.
    check("authz: a listed member may upload",
          web.IDENTITY_SOURCE_MEMBER in web.UPLOAD_ALLOWED_SOURCES)
    check("authz: a listed member still cannot inspect local paths",
          web.IDENTITY_SOURCE_MEMBER not in web.LOCAL_PATH_ALLOWED_SOURCES)
    check("authz: a guest still cannot upload",
          web.IDENTITY_SOURCE_GUEST not in web.UPLOAD_ALLOWED_SOURCES)

    # ── file types: sniffed from the bytes, non-images served as downloads ──
    def fetch(att_id):
        url = f"http://127.0.0.1:{port}/api/attachment/{att_id}?channel={CH}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()

    samples = {
        "report.pdf": (b"%PDF-1.7\n" + b"x" * 64, "application/pdf"),
        "logs.zip": (b"PK\x03\x04" + b"\x00" * 64, "application/zip"),
        "Budget.xlsx": (b"PK\x03\x04" + b"\x01" * 64, "application/zip"),
        "IMG_0001.HEIC": (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, "image/heic"),
        "notes.txt": ("Field notes: ✓ dome works\n".encode() * 3, "text/plain"),
    }
    for name, (payload, want) in samples.items():
        st, body = upload(port, payload, name)
        check(f"types: {name} accepted as {want}", st == 200 and body.get("mime") == want)
        if st == 200:
            code, headers, data = fetch(body["id"])
            disposition = headers.get("Content-Disposition", "")
            check(f"types: {name} is served as a download under its own name",
                  code == 200 and disposition.startswith("attachment;") and data == payload
                  and "UTF-8''" in disposition)
            check(f"types: {name} carries a sandbox CSP",
                  "sandbox" in headers.get("Content-Security-Policy", ""))
    # The saved name always ends in an extension the sniffed type allows, so a text
    # file cannot be saved as something that runs on a double-click.
    for sent, kept in (("run.bat", "run.bat.txt"), ("Invoice.hta", "Invoice.hta.txt"),
                       ("page.html", "page.html.txt"), ("log.CSV", "log.CSV")):
        st, body = upload(port, b"plain text\n", sent)
        check(f"names: text sent as {sent} is stored as {kept}",
              st == 200 and body.get("filename") == kept)
    st, body = upload(port, b"PK\x03\x04" + b"\x00" * 64, "tool.jar")
    check("names: a ZIP sent as tool.jar is stored as tool.jar.zip",
          st == 200 and body.get("filename") == "tool.jar.zip")
    st, body = upload(port, b"%PDF-1.4\n", "")
    check("names: an unnamed PDF is file.pdf", st == 200 and body.get("filename") == "file.pdf")
    check("sniff: a short file ending in a broken UTF-8 byte is not text",
          web.sniff_attachment_mime(b"abc\xff") is None)
    check("sniff: a long text file cut mid-character in the sample is text",
          web.sniff_attachment_mime(b"a" * 65535 + "é".encode() + b"tail") == "text/plain")
    check("env: a malformed NTH_UPLOAD_MAX_BYTES falls back to the default",
          web._env_bytes("NTH_TEST_NOT_SET_XYZ", 7) == 7)
    st, body = upload(port, b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64, "tool.bin")
    check("types: an unrecognised binary is refused (400)", st == 400)
    st, body = upload(port, PNG, "inline.png")
    code, headers, _data = fetch(body["id"])
    check("types: a PNG is still served inline",
          code == 200 and "Content-Disposition" not in headers)

    # ── per-member quota ────────────────────────────────────────────────────
    # 4096 bytes are already stored above; a 6 KB ceiling admits nothing more.
    _real_quota = nmedia.MAX_MEMBER_ATTACH_BYTES
    try:
        # Everything uploaded so far already exceeds this ceiling.
        nmedia.MAX_MEMBER_ATTACH_BYTES = 6144
        st, body = upload(port, PNG, "second.png")
        check("quota: upload past the per-member ceiling is refused (413)", st == 413)
        check("quota: refusal names the reason",
              "quota" in (body.get("error") or ""))

        # The ceiling is a SUM, not a per-request cap: raise it and the same
        # request succeeds. Without this, a test could pass against a bug that
        # rejects every second upload for any reason at all.
        nmedia.MAX_MEMBER_ATTACH_BYTES = 1024 * 1024
        st, _b = upload(port, PNG, "third.png")
        check("quota: same upload succeeds once the ceiling is raised", st == 200)
    finally:
        nmedia.MAX_MEMBER_ATTACH_BYTES = _real_quota

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
