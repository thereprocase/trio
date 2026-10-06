"""Tests for the working-indicator activity hook (nth_activity_hook.py).

The activity hook stamps sessions.last_seen on every PreToolUse / UserPromptSubmit
so the dashboard shows 'working' for the whole active turn — not just from the
agent's first trio call. Covers:
  * the hook stamps last_seen by fingerprint on both events
  * robustness: empty / non-JSON / non-dict / wrong-event stdin is a no-op, exit 0
  * unknown session_id is a harmless no-op
  * the payoff: a non-trio tool call (hook fire) after a turn end flips the
    roster status idle -> working, which is the exact bug this fixes.

Usage: python tests/test-working-indicator-activity.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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


def iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


_tmp = tempfile.mkdtemp(prefix="nth_act_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"
DB = str(srv.DB_PATH)

os.environ["CLAUDE_CODE_SESSION_ID"] = "act-sid-1"
r = json.loads(srv.nth_connect(summary="t", name="Worker", channel="act1"))
CH, agent = r["channel"], r["member_id"]


def raw():
    c = sqlite3.connect(DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def fire(payload, expect_exit=0, clear_session_env=False):
    """Run the hook with `payload` (str or dict) on stdin; assert exit code.

    clear_session_env strips CLAUDE_CODE_SESSION_ID / CLAUDE_SESSION_ID from the
    subprocess env so we can exercise the true 'no session_id anywhere' path
    (the hook otherwise falls back to those env vars by design)."""
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    env = {**os.environ, "NTH_DB_PATH": DB}
    if clear_session_env:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CLAUDE_SESSION_ID", None)
    p = subprocess.run([sys.executable, str(SERVER / "nth_activity_hook.py")],
                       input=payload, text=True, capture_output=True, env=env)
    return p.returncode == expect_exit


def last_seen(session_id):
    c = raw()
    try:
        row = c.execute("SELECT last_seen FROM sessions WHERE fingerprint=?",
                        (session_id,)).fetchone()
    finally:
        c.close()
    return row["last_seen"] if row else None


# ── the hook stamps sessions.last_seen on both events ────────────────────────
# Pin last_seen into the past, fire the hook, assert it advanced.
def pin_past():
    c = raw()
    try:
        c.execute("UPDATE sessions SET last_seen=? WHERE fingerprint=?", (iso(-60), "act-sid-1"))
    finally:
        c.close()


pin_past()
before = last_seen("act-sid-1")
check("PreToolUse stamps last_seen", fire({"session_id": "act-sid-1", "hook_event_name": "PreToolUse"}))
check("PreToolUse advanced last_seen", last_seen("act-sid-1") > before)

pin_past()
before = last_seen("act-sid-1")
check("UserPromptSubmit stamps last_seen",
      fire({"session_id": "act-sid-1", "hook_event_name": "UserPromptSubmit"}))
check("UserPromptSubmit advanced last_seen", last_seen("act-sid-1") > before)

# a payload with no hook_event_name still stamps (Claude Code may omit it; the
# settings.json registration already scopes which events reach us)
pin_past()
before = last_seen("act-sid-1")
check("missing event name still stamps", fire({"session_id": "act-sid-1"}))
check("missing event name advanced last_seen", last_seen("act-sid-1") > before)


# ── robustness: hostile / malformed stdin is a harmless no-op, exit 0 ────────
pin_past()
frozen = last_seen("act-sid-1")
check("empty stdin: exit 0, no-op", fire("") and last_seen("act-sid-1") == frozen)
check("non-JSON stdin: exit 0, no-op", fire("not json {") and last_seen("act-sid-1") == frozen)
check("non-dict JSON (list): exit 0, no-op", fire([1, 2, 3]) and last_seen("act-sid-1") == frozen)
check("non-dict JSON (string): exit 0, no-op", fire('"hi"') and last_seen("act-sid-1") == frozen)
check("wrong event name: defensive no-op",
      fire({"session_id": "act-sid-1", "hook_event_name": "Stop"}) and last_seen("act-sid-1") == frozen)
check("no session_id anywhere (env cleared): no-op",
      fire({"hook_event_name": "PreToolUse"}, clear_session_env=True)
      and last_seen("act-sid-1") == frozen)
check("no session_id in payload: env fallback stamps (by design)",
      fire({"hook_event_name": "PreToolUse"}) and last_seen("act-sid-1") > frozen)
# restore the frozen baseline for any later assertions
c = raw()
try:
    c.execute("UPDATE sessions SET last_seen=? WHERE fingerprint=?", (frozen, "act-sid-1"))
finally:
    c.close()
check("unknown session_id: harmless no-op",
      fire({"session_id": "act-sid-nobody", "hook_event_name": "PreToolUse"})
      and last_seen("act-sid-nobody") is None)


# ── the payoff: a tool call after a turn end flips idle -> working ────────────
hub = web.EventHub(srv.DB_PATH, CH)


def status_of(member_id):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    try:
        roster = hub._fetch_roster(c)
    finally:
        c.close()
    for m in roster:
        if m["id"] == member_id:
            return m["status"]
    return None


# Set up the exact bug scenario: the agent's last activity predates its last
# turn end (it finished a turn and is now reasoning on a new one WITHOUT having
# made a trio call yet) -> idle.
c = raw()
try:
    c.execute("UPDATE members SET last_seen=? WHERE channel=? AND id=?", (iso(0), CH, agent))
    c.execute("UPDATE sessions SET last_seen=?, last_turn_end=? WHERE fingerprint=?",
              (iso(-20), iso(-5), "act-sid-1"))
finally:
    c.close()
check("before hook: reasoning with no trio call reads idle", status_of(agent) == "idle")

# Now a plain tool call fires the activity hook (NOT a trio call) -> last_seen
# jumps past last_turn_end -> working. This is the whole point of the feature.
check("activity hook fires on a tool call",
      fire({"session_id": "act-sid-1", "hook_event_name": "PreToolUse"}))
check("after hook: tool-call activity flips idle -> working", status_of(agent) == "working")


os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
# ── fingerprint scoping: a session id is NOT unique to a member ─────────────
# nth_connect mints a fresh member_id on every connect and never revokes the old
# session row, so one fingerprint accumulates a row per reconnect. An unscoped
# UPDATE stamps them all, resurrecting long-dead members as "working".
_fp = "fp-scope-test"
_db = sqlite3.connect(str(DB))
try:
    _now = datetime.now(timezone.utc)
    def _iso(sec):
        return (_now - timedelta(seconds=sec)).isoformat()
    for tok, mid, ch, conn_at in (
        ("sc1", "stale_reconnect_1", "scopeA", _iso(9000)),
        ("sc2", "stale_reconnect_2", "scopeA", _iso(6000)),
        ("sc3", "live_a",            "scopeA", _iso(10)),
        ("sc4", "live_b",            "scopeB", _iso(10)),
    ):
        _db.execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_token, member_id, channel, role, fingerprint, connected_at, last_seen) "
            "VALUES (?,?,?,'primary',?,?,?)", (tok, mid, ch, _fp, conn_at, _iso(3000)))
    _db.commit()
finally:
    _db.close()

os.environ["CLAUDE_CODE_SESSION_ID"] = _fp
fire({"tool_name": "Bash"})

_db = sqlite3.connect(str(DB))
try:
    seen = dict(_db.execute(
        "SELECT member_id, last_seen FROM sessions WHERE fingerprint = ?", (_fp,)).fetchall())
finally:
    _db.close()
_cut = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
_fresh = {m for m, ls in seen.items() if ls and ls > _cut}
check("scoping: stale reconnect rows are NOT stamped",
      "stale_reconnect_1" not in _fresh and "stale_reconnect_2" not in _fresh)
check("scoping: the live session in each channel IS stamped",
      _fresh == {"live_a", "live_b"})

shutil.rmtree(_tmp, ignore_errors=True)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
