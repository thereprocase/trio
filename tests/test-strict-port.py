"""--strict-port: refuse to land somewhere other than the port you asked for.

By default the hub scans upward from --port for the first free one, which is
right for a human starting a second dashboard by hand. It is wrong for every
non-interactive caller, because the whole point of those is that the port is
written down somewhere else: a LaunchAgent/systemd unit, a registered MCP
endpoint, a bookmark. When the hub silently lands on port+1, every one of
those keeps pointing at an address nothing is listening on, and the hub itself
reports success — there is no error anywhere to notice.

So the contract has two halves and both are pinned here:

  * WITH the flag, a busy port is a hard failure. Exit non-zero, and say on
    stderr that the port was busy AND that no other port was tried — an
    operator reading a service log needs to know the scan did not silently
    happen.
  * WITHOUT the flag, the scan still works. This is a real regression risk:
    the obvious implementation of "strict" is to change the loop bound, and
    getting that backwards disables the fallback for everybody.

Usage: python tests/test-strict-port.py
"""
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
WEB = SERVER / "nth_web.py"
sys.path.insert(0, str(SERVER))
import nth_server as srv    # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def occupy():
    """Hold a real port so the hub genuinely cannot bind it."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


tmp = Path(tempfile.mkdtemp(prefix="nth_strict_port_"))


def fresh_db(name):
    """The hub refuses to start without an existing DB, so build a real one."""
    path = tmp / name
    srv.DB_DIR = tmp
    srv.DB_PATH = path
    srv.get_db().close()
    return path

# --- with the flag: a busy port must stop the process ----------------------
held, port = occupy()
try:
    # A timeout here is not an infrastructure hiccup, it IS the failure: if the
    # flag is ignored the hub binds port+1 and serves forever, so "never
    # returned" and "silently landed elsewhere" are the same observation.
    started_anyway = False
    try:
        proc = subprocess.run(
            [sys.executable, str(WEB), "--db", str(fresh_db("a.db")),
             "--port", str(port), "--strict-port"],
            capture_output=True, text=True, timeout=60)
        err = proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        started_anyway = True
        err, rc = "", 0

    check("--strict-port did not exit by starting a server on some other port",
          not started_anyway)
    check("--strict-port is an accepted flag",
          "unrecognized arguments" not in err)
    check("a busy port under --strict-port exits non-zero", rc != 0)
    check("the error names the port that was busy", str(port) in err)
    check("the error says no other port was tried",
          "no other port was tried" in err.lower())
    # The failure must be legible without the flag name being the only clue,
    # because this lands in a service log read hours later.
    check("the error suggests what to do about it",
          "stop it" in err.lower() or "different --port" in err.lower())
finally:
    held.close()

# --- without the flag: the scan must still work ----------------------------
# Guard against the natural way to break this: inverting the loop bound.
held, port = occupy()
proc = None
try:
    proc = subprocess.Popen(
        [sys.executable, str(WEB), "--db", str(fresh_db("b.db")), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    moved = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if listening(port + 1):
            moved = True
            break
        time.sleep(0.25)
    check("without the flag a busy port still falls through to the next one", moved)
    check("the fallback process is actually running, not merely exited",
          proc.poll() is None)
finally:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    held.close()

print()
if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK")
