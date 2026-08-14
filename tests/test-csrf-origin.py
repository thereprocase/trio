"""Tests for the cross-site POST rejection in NthWebHandler.do_POST.

Why this exists: identity is derived from the SOURCE IP, not from the session
cookie (_resolve_identity mints a fresh token for a cookie-less request and
then resolves it via tailscale whois / loopback). So SameSite is not a CSRF
control here -- a cross-origin fetch carrying no cookie still resolves as the
trusted operator. With a CORS-safelisted Content-Type (text/plain) the request
also skips preflight, so the write lands before anything can object; the
attacker never reads the response and does not need to.

The four shapes below are the whole contract:
  A  no Origin at all           -> allowed  (curl, scripts, the MCP side)
  B  Origin != Host             -> 403      (the attack)
  C  Sec-Fetch-Site: cross-site -> 403      (defence in depth, no Origin sent)
  D  Origin == Host             -> allowed  (the real dashboard in a browser)

A and D are not decoration: a check that only proves B and C is equally
satisfied by rejecting every POST, which would break every non-browser client
and the dashboard itself.

Usage: python tests/test-csrf-origin.py
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


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_csrf_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def post(port, path, body, headers):
    """POST with an explicitly controlled header set.

    Content-Type is a parameter, not a constant: text/plain is the CORS-simple
    value an attacker page would use precisely because it avoids a preflight.
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


r = json.loads(srv.nth_connect(summary="t", name="R", channel="csrftest"))
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

    host = f"127.0.0.1:{port}"

    # A — no Origin. Non-browser clients send none; they must keep working.
    st, resp = post(port, "/api/send", {"content": "csrf-A"},
                    {"Content-Type": "text/plain"})
    check("A: POST without Origin is allowed", st == 200 and resp.get("ok") is True)

    # B — the attack: a page on another origin, no cookie, simple Content-Type.
    st, resp = post(port, "/api/send", {"content": "csrf-B"},
                    {"Content-Type": "text/plain", "Origin": "https://evil.example"})
    check("B: cross-origin POST is rejected", st == 403)
    check("B: message was NOT stored", "csrf-B" not in json.dumps(
        json.loads(srv.nth_history(channel=CH, last_n=50))))

    # C — some clients omit Origin but send Sec-Fetch-Site.
    st, _ = post(port, "/api/send", {"content": "csrf-C"},
                 {"Content-Type": "text/plain", "Sec-Fetch-Site": "cross-site"})
    check("C: Sec-Fetch-Site cross-site is rejected", st == 403)

    # D — the real dashboard. Origin matches Host exactly.
    st, resp = post(port, "/api/send", {"content": "csrf-D"},
                    {"Content-Type": "application/json",
                     "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})
    check("D: same-origin POST is allowed", st == 200 and resp.get("ok") is True)

    # The check must cover EVERY state-changing route, not just /api/send --
    # the filesystem-touching ones are the reason it exists.
    for route, body in (("/api/reveal", {"path": "/etc"}),
                        ("/api/path/validate", {"paths": ["/etc"]}),
                        ("/api/identify", {"name": "mallory"})):
        st, _ = post(port, route, body,
                     {"Content-Type": "text/plain", "Origin": "https://evil.example"})
        check(f"cross-origin POST to {route} is rejected", st == 403)

finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    hub.stop()

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all CSRF origin checks passed")
