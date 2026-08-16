"""Every roster event must name its channel.

The client subscribes to TWO SSE streams: the per-channel one, and (for an
operator) a workspace-wide one that multiplexes every channel's hub. Because
the second exists, web/js/04-events.js applies a roster only when it belongs to
the channel currently on screen:

    if (type === 'roster' && payload.channel === Trio.state.channel) …

Without that guard, the workspace stream's agent-inbox roster — which lists
every agent ever created — replaces the open channel's member list. With the
guard but WITHOUT the field, `undefined === "smoke"` is false and the roster is
never applied AT ALL.

That second failure is the one this pins, because it is silent and it is what
actually shipped: the guard was added on the client while the per-channel
emissions still went out unstamped. Nothing errors. The member map simply stays
empty, and everything downstream of a member NAME degrades — @mention chips
render as plain text, the facepile is blank, and nameFor() falls back to raw
member ids like "567wge" in the UI.

Both emission sites are covered: the initial roster a new subscriber is sent,
and the change-detected broadcast. They are ~200 lines apart and only one was
stamped once already.

Usage: python tests/test-sse-roster-channel.py
"""
import json
import re
import shutil
import sys
import tempfile
import threading
import time
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


# ── 1. Source-level: neither emission site may omit the field ──────────────
# Cheap, and it covers the broadcast site without having to race a change
# detector in a live stream.
source = (SERVER / "nth_web.py").read_text(encoding="utf-8")
emissions = re.findall(r'\{"type": "roster",(.*?)\}', source, re.DOTALL)
check(f"both roster emission sites were found ({len(emissions)})",
      len(emissions) == 2)
unstamped = [e for e in emissions if '"channel"' not in e]
check("every roster emission stamps its channel"
      + (f" — {len(unstamped)} does not" if unstamped else ""),
      not unstamped)

# ── 2. Behavioural: the field survives to the wire ─────────────────────────
_tmp = tempfile.mkdtemp(prefix="nth_roster_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"

r = json.loads(srv.nth_connect(summary="t", name="Ada", channel="rosterch"))
CH, ada = r["channel"], r["member_id"]
srv.nth_send(channel=CH, member_id=ada, message="hello")

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
    time.sleep(0.3)

    rosters = []
    deadline = time.time() + 8
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/events?channel={CH}", timeout=8) as resp:
        while time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                continue
            try:
                payload = json.loads(text[5:].strip())
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("type") == "roster":
                rosters.append(payload)
                break

    check("the stream delivered a roster event", bool(rosters))
    if rosters:
        got = rosters[0].get("channel")
        check(f"the roster names its channel (got {got!r}, expected {CH!r}) — "
              "the client compares this to the channel on screen and drops "
              "any roster that does not match, so an absent field means the "
              "member list is never populated at all",
              got == CH)
        check("and still carries the members themselves",
              any(m.get("name") == "Ada" for m in rosters[0].get("members", [])))
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    hub.stop()
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("OK — roster events name their channel, so the client can apply them")
