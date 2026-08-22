"""Every SSE event must name its channel.

The client subscribes to TWO streams: the per-channel one, and (for an
operator) a workspace-wide one that merges EVERY channel's hub queue into a
single connection. Because the second exists, the client decides where an event
belongs by comparing its `channel` to the room on screen:

    roster   04-events.js:33          payload.channel === Trio.state.channel
    message  11-conversation.js:736   msg.channel && … !== state.channel

Both guards FAIL OPEN when the field is missing — the first compares against
`undefined` and never matches, the second short-circuits and never rejects. So
an unstamped event is neither dropped nor logged; it is silently mishandled, in
opposite directions depending on the type:

  * roster unstamped  → the member map is never populated at all, so @mention
    chips render as plain text, the facepile is blank, and nameFor() shows raw
    member ids like "567wge".
  * message unstamped → every OTHER channel's messages render into whatever
    conversation the operator has open, join notices included. The same missing
    field makes channel mute a no-op (45-notifications.js keys the mute on it)
    and stops the cross-channel desktop popup from ever firing.

This is the defect the port kept reproducing: the client's guards came across
from the fork, the server-side stamping did not. It was then fixed for `roster`
ALONE while three message-event sites in the same file still went out bare —
which is why this test covers the event TYPE rather than one event.

Every emission site is covered: the roster snapshot a new subscriber receives,
the roster change broadcast, the history burst, the live message tail, and the
edit/retraction `message_update` path.

Usage: python tests/test-sse-event-channel.py
"""
import inspect
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


# ── 1. Source-level ────────────────────────────────────────────────────────
# Cheap, and it reaches sites a live stream can only hit by racing a change
# detector (the roster broadcast and the message_update path).
source = (SERVER / "nth_web.py").read_text(encoding="utf-8")

emissions = re.findall(r'\{"type": "roster",(.*?)\}', source, re.DOTALL)
check(f"both roster emission sites were found ({len(emissions)})",
      len(emissions) == 2)
unstamped = [e for e in emissions if '"channel"' not in e]
check("every roster emission stamps its channel"
      + (f" — {len(unstamped)} does not" if unstamped else ""),
      not unstamped)

# Message events go through one builder, so the durable guard is that the
# builder REQUIRES the channel positionally rather than that each of its four
# callers remembered to pass it. A default value would silently restore the
# bug, so the absence of one is the thing asserted.
sig = inspect.signature(web._message_event)
params = list(sig.parameters.values())
check(f"_message_event takes channel as a parameter ({sig})",
      "channel" in sig.parameters)
check("…and it is REQUIRED — a default would let a new call site omit it and "
      "reintroduce cross-channel rendering silently",
      sig.parameters["channel"].default is inspect.Parameter.empty
      if "channel" in sig.parameters else False)
check("…and positional, so a missed call site is a TypeError at the call "
      "rather than a wrong message three screens away",
      params[-1].kind is not inspect.Parameter.KEYWORD_ONLY if params else False)

calls = re.findall(r"_message_event\((.*?)\)", source)
bare = [c for c in calls if c.count(",") < 2]
check(f"every _message_event call site passes a channel ({len(calls)} found)"
      + (f" — bare: {bare}" if bare else ""),
      len(calls) >= 4 and not bare)

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

    # Three separate code paths emit a message event, and reading only the
    # history burst leaves two of them untested — passing a bare "" from the
    # LIVE TAIL survived an earlier version of this test. So: subscribe, take
    # the burst, then post a new message and an edit while the stream is open,
    # and require the channel on each one as it arrives.
    seen = {}
    live = {"posted": False}

    def post_later():
        time.sleep(1.0)
        mid = json.loads(srv.nth_send(channel=CH, member_id=ada,
                                      message="LIVE TAIL"))
        time.sleep(0.8)
        srv.nth_retract(channel=CH, member_id=ada,
                        message_id=mid.get("id") or mid.get("message_id"),
                        reason="testing message_update")
        live["posted"] = True

    threading.Thread(target=post_later, daemon=True).start()

    deadline = time.time() + 12
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/events?channel={CH}", timeout=12) as resp:
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
            if not isinstance(payload, dict) or not payload.get("type"):
                continue
            kind = payload["type"]
            # Distinguish the burst from the live tail by content, so the two
            # do not collapse into one bucket and hide each other.
            if kind == "message" and payload.get("content") == "LIVE TAIL":
                kind = "message:live"
            seen.setdefault(kind, payload)
            if {"roster", "message", "message:live",
                "message_update"} <= set(seen):
                break

    check(f"the stream delivered every event type under test "
          f"(got {sorted(seen)})",
          {"roster", "message", "message:live", "message_update"} <= set(seen))

    if "roster" in seen:
        got = seen["roster"].get("channel")
        check(f"the roster names its channel (got {got!r}, expected {CH!r}) — "
              "an absent field means the member list is never populated at all",
              got == CH)
        check("and still carries the members themselves",
              any(m.get("name") == "Ada"
                  for m in seen["roster"].get("members", [])))

    # Each message-bearing path asserted separately: they are three different
    # call sites and a fix applied to one is not a fix applied to the others.
    for label, key in (("history burst", "message"),
                       ("live tail", "message:live"),
                       ("edit/retraction update", "message_update")):
        if key not in seen:
            continue
        got = seen[key].get("channel")
        check(f"the {label} names its channel (got {got!r}, expected {CH!r}) — "
              "an absent or empty field short-circuits the client's "
              "cross-channel guard, so it renders into whatever room is open",
              got == CH)
    if "message" in seen:
        check("and the message still carries its content",
              seen["message"].get("content") is not None)
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
print("OK — every SSE event names its channel, so the client can place it")
