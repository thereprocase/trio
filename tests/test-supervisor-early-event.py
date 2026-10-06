#!/usr/bin/env python3
"""Regression: supervisor must register a proc in _procs BEFORE start().

The stale-source guard in _handle_event drops any event whose `source` proc
isn't the one currently in _procs (correct for late events from dead procs).
But spawn() used to call proc.start() BEFORE self._procs[agent_id] = proc, so
an event emitted by the reader thread in that window was dropped — the init
event's hub forwarding (and any very early assistant event) was lost. The fix
registers the proc first, then starts.

This test wraps AgentProc.start to synchronously fire an init event via the
on_event callback BEFORE start returns (simulating a very fast agent), and
asserts the event reaches the supervisor's on_event hook (hub forward). With
the old (start-then-register) order the guard dropped it.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ["TRIO_AGENT_CMD"] = f"{sys.executable} {HERE / 'fake_agent.py'}"

import nth_server as srv  # noqa: E402
import nth_supervisor as sup  # noqa: E402

# Crash the real agent immediately so it emits NO init of its own — the only
# init event in play is the synthetic one fired by patched_start. That makes
# the assertion unambiguous: forwarded ⇔ the synthetic event passed the guard.
os.environ["FAKE_AGENT_CRASH"] = "1"

failed = []


def check(label, ok):
    print(("PASS" if ok else "FAIL") + ": " + label)
    if not ok:
        failed.append(label)


tmp = Path(tempfile.mkdtemp(prefix="nth_early_evt_"))
srv.DB_DIR = tmp
srv.DB_PATH = tmp / "nth.db"
json.loads(srv.nth_connect(summary="host", name="Host", channel="early"))

aid = "ag_early"
now = srv.now_iso()
db = srv.get_db()
db.execute("INSERT INTO agents (id,name,model,state,managed,created_at) "
           "VALUES (?, 'Early', 'sonnet', 'stopped', 1, ?)", (aid, now))
db.execute("INSERT INTO members (id,channel,name,summary,skills,last_seen,joined_at,active,kind) "
           "VALUES (?, 'early', 'Early','','',?,?,1,'agent')", (aid, now, now))
db.execute("INSERT INTO agent_channels (agent_id,channel,member_id,joined_at) "
           "VALUES (?, 'early', ?, ?)", (aid, aid, now))
db.commit()
db.close()

# Capture hub-forwarded events via the supervisor's on_event hook.
seen_events = []


def hub_hook(agent_id, evt):
    seen_events.append((agent_id, evt.get("type"), evt.get("subtype")))


# Wrap AgentProc.start so that, BEFORE the real reader threads run, we
# synchronously fire an init event through on_event — exactly the race window
# (event delivered while spawn() hasn't returned to register the proc). With
# the fix, _procs already holds the proc, so the guard admits it.
_orig_start = sup.AgentProc.start


def patched_start(self):
    # The supervisor has already constructed `self` with on_event bound to
    # _handle_event(source=proc). Fire a synthetic init event NOW, before the
    # real process starts. If _procs doesn't hold this proc yet (old order),
    # the guard in _handle_event drops it.
    if self.on_event is not None:
        try:
            self.on_event(self.agent_id, {
                "type": "system", "subtype": "init",
                "session_id": "synthetic-early-init", "model": "sonnet",
            })
        except Exception:
            pass
    return _orig_start(self)


sup.AgentProc.start = patched_start

try:
    sv = sup.AgentSupervisor(db_path=srv.DB_PATH)
    sv.on_event = hub_hook
    proc = sv.spawn(aid, model="sonnet", session_timeout=2.0)
    # Give the real reader a moment to flush, then shut down.
    import time
    time.sleep(0.3)
    sv.shutdown(preserve_sessions=False)

    init_forwarded = any(
        aid == a and t == "system" and st == "init"
        for (a, t, st) in seen_events
    )
    check("early init event forwarded to hub (proc registered before start)",
          init_forwarded)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{'FAILED' if failed else 'OK'} — {len(failed)} failure(s)")
sys.exit(1 if failed else 0)
