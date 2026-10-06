"""Tests for clickable-file-path endpoints (POST /api/path/validate, /api/reveal).

Real temp files → validate reports existence and reveal builds a SAFE Finder
call; missing paths → exists=false / 404; injection-style values ("; rm -rf ~",
a leading-dash "--flag") never reach a shell and never launch anything. The
actual `open -R` is MOCKED (web.subprocess.run) so the test asserts the exact
arg list without popping a Finder window, exercising the whole validation +
arg-construction path.
Usage: python tests/test-file-reveal.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from urllib.parse import quote
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


def skip(name, why):
    print(f"SKIP: {name} ({why})")
    skips.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_reveal_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"

# A real file to reveal, and a real directory.
real_file = os.path.join(_tmp, "hello world.txt")
with open(real_file, "w") as f:
    f.write("hi")
missing = os.path.join(_tmp, "does-not-exist.txt")

# Mock the Finder launch: record the exact call, never spawn `open`.
reveal_calls = []


# Linux reveal has two tiers: a D-Bus FileManager1.ShowItems call that SELECTS
# the file (what macOS and Windows do), falling back to xdg-open on the
# containing folder. Flipping this lets a test force the D-Bus tier to fail so
# the fallback is exercised too. A mocked test cannot prove a desktop accepts
# the D-Bus call — that is test-reveal-realtool.py's job — but it can and must
# prove we fall back when the call fails.
dbus_returncode = [0]


def fake_run(args, **kwargs):
    # Record only reveal invocations. identity resolution also goes through
    # subprocess (tailscale whois via check_output, which is built on run), and
    # counting those would break every "no exec" assertion below.
    if args and args[0] in ("open", "xdg-open", "explorer", "explorer.exe", "dbus-send"):
        reveal_calls.append({"args": args, "kwargs": kwargs})
    # Real subprocess.run returns BYTES for stdout unless text=True, and
    # check_output() is implemented on top of run() — returning str here makes
    # every check_output caller (e.g. tailscale_whois) blow up on .decode().
    text = kwargs.get("text") or kwargs.get("universal_newlines")
    empty = "" if text else b""
    rc = dbus_returncode[0] if (args and args[0] == "dbus-send") else 0
    return types.SimpleNamespace(returncode=rc, stdout=empty, stderr=empty)


web.subprocess.run = fake_run


def http(port, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


r = json.loads(srv.nth_connect(summary="t", name="R", channel="revealtest"))
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

    # ── /api/path/validate ──
    st, resp = http(port, "/api/path/validate", {"paths": [real_file, missing, "~"]})
    check("validate: 200", st == 200)
    ex = resp.get("exists", {})
    check("validate: real file exists=true", ex.get(real_file) is True)
    check("validate: missing exists=false", ex.get(missing) is False)
    check("validate: ~ expands and exists=true", ex.get("~") is True)

    # A `path:line[:col]` token (Claude-Code form) must validate true when the
    # bare file exists — keyed under the ORIGINAL token so the client key lines
    # up. (Regression guard: validate must strip the suffix like reveal does.)
    st, resp = http(port, "/api/path/validate",
                    {"paths": [real_file + ":42", real_file + ":42:7"]})
    ex = resp.get("exists", {})
    check("validate: path:line token exists=true", ex.get(real_file + ":42") is True)
    check("validate: path:line:col token exists=true", ex.get(real_file + ":42:7") is True)

    # A bare '/' (and other trivial roots / pure-separator tokens) EXIST on disk
    # but must never validate as linkable — otherwise a slash used as prose
    # punctuation ("reload / incognito") picks up a folder link. Defense in depth
    # behind the client's filename-segment candidate filter.
    roots = ["/", "//", "/..", "   "]
    st, resp = http(port, "/api/path/validate", {"paths": roots})
    ex = resp.get("exists", {})
    check("validate: bare '/' exists=false", ex.get("/") is False)
    check("validate: '//' exists=false", ex.get("//") is False)
    check("validate: '/..' (root) exists=false", ex.get("/..") is False)
    # A real path UNDER the root still validates (rejection is roots-only).
    st, resp = http(port, "/api/path/validate", {"paths": ["/", real_file]})
    ex = resp.get("exists", {})
    check("validate: real path under root still exists=true",
          ex.get("/") is False and ex.get(real_file) is True)

    # Injection-style tokens are just non-existent strings — never executed.
    inj = ['"; rm -rf ~"', "--flag", "-R", "$(whoami)", "a\x00b"]
    st, resp = http(port, "/api/path/validate", {"paths": inj})
    check("validate: injection tokens all exist=false",
          st == 200 and all(v is False for v in resp.get("exists", {}).values()))

    # Bad input shapes.
    st, _ = http(port, "/api/path/validate", {"paths": "notalist"})
    check("validate: non-list paths -> 400", st == 400)
    st, resp = http(port, "/api/path/validate", {"paths": []})
    check("validate: empty list -> 200 empty map", st == 200 and resp.get("exists") == {})

    # Cap: 205 unique missing paths + the real one; capped at 200, but the real
    # one is first so it's within the cap and still validated true.
    many = [real_file] + [f"/nope/{i}" for i in range(205)]
    st, resp = http(port, "/api/path/validate", {"paths": many})
    ex = resp.get("exists", {})
    check("validate: caps candidates at 200", st == 200 and len(ex) <= 200)
    check("validate: real file still true under cap", ex.get(real_file) is True)

    # ── /api/reveal ──
    reveal_calls.clear()
    st, resp = http(port, "/api/reveal", {"path": real_file})
    check("reveal: real file -> 200 ok", st == 200 and resp.get("ok") is True)
    if reveal_calls:
        call = reveal_calls[-1]
        args = call["args"]
        check("reveal: subprocess called with an ARG LIST (no shell string)",
              isinstance(args, list))
        check("reveal: no shell=True", call["kwargs"].get("shell") in (None, False))
        # Assert the argv on EVERY platform, not just darwin. The original
        # version skipped everything here off macOS, which is exactly why two
        # broken argv forms shipped: on Linux the `--` made every call fail, and
        # on Windows "/select," and the path were separate tokens. A mocked test
        # cannot tell you the OS accepts an argv -- that is what
        # tests/test-reveal-realtool.py is for -- but it can and must pin the
        # argv we intend to send.
        if sys.platform == "darwin":
            check("reveal: uses `open -R` (reveal, not launch)",
                  args[:2] == ["open", "-R"])
            check("reveal: `--` guards against flag injection", "--" in args)
            check("reveal: reveals the abspath of the real file",
                  args[-1] == os.path.abspath(real_file))
        elif sys.platform.startswith("linux"):
            # Tier 1: D-Bus ShowItems, which SELECTS the file the way macOS and
            # Windows do. Only attempted when dbus-send exists and a session bus
            # is advertised, so on a headless box we land on xdg-open instead
            # and assert that below.
            if args[0] == "dbus-send":
                check("reveal: D-Bus targets FileManager1",
                      "--dest=org.freedesktop.FileManager1" in args)
                check("reveal: D-Bus calls ShowItems (select, not open)",
                      "org.freedesktop.FileManager1.ShowItems" in args)
                uri_arg = [a for a in args if a.startswith("array:string:")]
                check("reveal: D-Bus is passed one file:// URI",
                      len(uri_arg) == 1
                      and uri_arg[0] == "array:string:file://"
                          + quote(os.path.abspath(real_file)))
                # dbus-send splits `array:` arguments on COMMAS, and a comma is
                # a legal filename character. quote() encodes it as %2C, so a
                # raw comma can never reach that parser.
                check("reveal: no raw comma reaches the D-Bus array parser",
                      "," not in uri_arg[0][len("array:string:"):])
                check("reveal: D-Bus call is bounded by a reply timeout",
                      any(a.startswith("--reply-timeout=") for a in args))
            else:
                check("reveal: uses xdg-open", args[0] == "xdg-open")
                check("reveal: opens the containing folder of the real file",
                      args[-1] == os.path.dirname(os.path.abspath(real_file)))
            # Regression guard, both tiers. xdg-open's arg loop matches "-*"
            # first and rejects "--" outright, so its presence broke every
            # Linux reveal.
            check("reveal: NO `--` (xdg-open rejects it as an unknown option)",
                  "--" not in args)

            # Tier 2: when D-Bus fails, we MUST fall back to xdg-open rather
            # than reporting failure. This is the half a mocked test can prove,
            # and the reason the D-Bus tier is safe to add at all.
            reveal_calls.clear()
            dbus_returncode[0] = 1
            try:
                st_fb, resp_fb = http(port, "/api/reveal", {"path": real_file})
            finally:
                dbus_returncode[0] = 0
            fb = [c["args"] for c in reveal_calls if c["args"][0] == "xdg-open"]
            check("reveal: D-Bus failure falls back to xdg-open", len(fb) == 1)
            check("reveal: fallback still returns 200",
                  st_fb == 200 and resp_fb.get("ok") is True)
            if fb:
                check("reveal: fallback opens the containing folder",
                      fb[0][-1] == os.path.dirname(os.path.abspath(real_file)))
                check("reveal: fallback carries NO `--`", "--" not in fb[0])
        elif sys.platform.startswith("win"):
            check("reveal: uses explorer", args[0] == "explorer")
            # Regression guard. "/select," and the path must be ONE token; split
            # across two, explorer ignores the selector and opens Documents.
            check("reveal: `/select,<path>` is a SINGLE argv token",
                  len(args) == 2 and args[1] == f"/select,{os.path.abspath(real_file)}")
        else:
            skip("reveal: argv assertions", f"unsupported platform {sys.platform}")
    else:
        check("reveal: subprocess.run was invoked", False)

    # path:line[:col] suffix is stripped before revealing.
    reveal_calls.clear()
    st, resp = http(port, "/api/reveal", {"path": real_file + ":42:7"})
    check("reveal: path:line:col accepted -> 200", st == 200)
    if reveal_calls and sys.platform == "darwin":
        check("reveal: :line:col stripped, file revealed",
              reveal_calls[-1]["args"][-1] == os.path.abspath(real_file))

    # Missing path → 404 and NO subprocess call at all.
    reveal_calls.clear()
    st, resp = http(port, "/api/reveal", {"path": missing})
    check("reveal: missing path -> 404", st == 404)
    check("reveal: missing path never invokes open", not reveal_calls)

    # A bare '/' (and pure-separator roots) exist on disk but must be REFUSED —
    # never revealed — so a slash-as-punctuation link can't open the root folder.
    for root in ["/", "//", "/.."]:
        reveal_calls.clear()
        st, _ = http(port, "/api/reveal", {"path": root})
        check(f"reveal: trivial root {root!r} -> 404, no exec",
              st == 404 and not reveal_calls)

    # A leading-dash / injection value that doesn't exist → 404, open untouched.
    for bad in ["--flag", "-R", '"; rm -rf ~"', "$(whoami)"]:
        reveal_calls.clear()
        st, _ = http(port, "/api/reveal", {"path": bad})
        check(f"reveal: injection {bad!r} -> 404, no exec",
              st == 404 and not reveal_calls)

    # Empty / missing / non-string path → 400.
    for bad in ({"path": ""}, {"path": "   "}, {"path": 123}, {}):
        st, _ = http(port, "/api/reveal", bad)
        check(f"reveal: bad body {bad} -> 400", st == 400)


    # ── relative candidates never resolve ───────────────────────────────────
    # They would resolve against the SERVER's cwd, not the cwd of the agent that
    # wrote the message, so a link would confidently open a file from whichever
    # checkout the dashboard was launched next to.
    st, b = http(port, "/api/path/validate",
                 {"paths": ["server/nth_web.py", "./setup.sh", "../x", "/etc/hosts"]})
    ex = (b or {}).get("exists", {})
    check("relative: bare relative path does not resolve", ex.get("server/nth_web.py") is False)
    check("relative: ./ path does not resolve", ex.get("./setup.sh") is False)
    check("relative: ../ path does not resolve", ex.get("../x") is False)
    check("absolute paths still resolve", ex.get("/etc/hosts") is True)
    st, b = http(port, "/api/reveal", {"path": "server/nth_web.py"})
    check("relative: reveal refuses a relative path", st == 404)

    # ── access control ──────────────────────────────────────────────────────
    # These endpoints answer questions about the OPERATOR'S OWN filesystem and
    # can pop Finder windows on their screen. Untrusted tiers must be refused;
    # without this the whole gate can be deleted and the suite stays green.
    _real = web.NthWebHandler._resolve_identity
    try:
        class _Guest:
            source = web.IDENTITY_SOURCE_GUEST
            name = "guest"
            summary = "guest"
        web.NthWebHandler._resolve_identity = lambda self: (None, _Guest(), False)
        st, _b = http(port, "/api/path/validate", {"paths": ["/etc/hosts"]})
        check("authz: guest cannot enumerate paths (403)", st == 403)
        st, _b = http(port, "/api/reveal", {"path": "/etc/hosts"})
        check("authz: guest cannot reveal a path (403)", st == 403)
    finally:
        web.NthWebHandler._resolve_identity = _real

except OSError as e:
    skip("file-reveal", f"could not start server: {e}")
finally:
    if server is not None:
        server.shutdown()
    hub.stop()

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), {len(skips)} skip(s)")
sys.exit(1 if failures else 0)
