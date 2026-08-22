"""Tests for the agent working/idle indicator (feat/working-indicator).

Covers member_status()'s working/idle/active split, the nth_turn_hook stamping
sessions.last_turn_end, and the roster integration end to end:
  working — alive AND acted since its last turn end (mid-turn)
  idle    — alive AND its last turn ended (waiting) / sleeping status_text
  active  — alive but no turn data (hook not installed) — legacy, no regression

Usage: python tests/test-working-indicator.py
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


# ── member_status() unit cases ───────────────────────────────────────────────
check("dead: no last_seen", web.member_status(None, "") == "dead")
check("dead: heartbeat > DEAD_SECONDS", web.member_status(iso(-1000), "") == "dead")
check("stale: heartbeat aging", web.member_status(iso(-400), "") == "stale")
check("idle: sleeping status_text wins", web.member_status(iso(-5), "idle — standing by") == "idle")
check("active: alive, no turn data (hook not installed)", web.member_status(iso(-5), "") == "active")
check("working: acted since last turn end",
      web.member_status(iso(-5), "", session_activity_iso=iso(-3), last_turn_end_iso=iso(-30)) == "working")
check("idle: last turn ended after last activity",
      web.member_status(iso(-5), "", session_activity_iso=iso(-30), last_turn_end_iso=iso(-3)) == "idle")
check("idle: turn ended but no activity recorded",
      web.member_status(iso(-5), "", session_activity_iso=None, last_turn_end_iso=iso(-3)) == "idle")
check("dead precedence over turn data",
      web.member_status(iso(-1000), "", session_activity_iso=iso(-1), last_turn_end_iso=iso(-30)) == "dead")


# ── the turn hook stamps sessions.last_turn_end ──────────────────────────────
_tmp = tempfile.mkdtemp(prefix="nth_wi_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"
DB = str(srv.DB_PATH)

os.environ["CLAUDE_CODE_SESSION_ID"] = "wi-sid-1"
r = json.loads(srv.nth_connect(summary="t", name="Worker", channel="wi1"))
CH, agent = r["channel"], r["member_id"]


def raw():
    c = sqlite3.connect(DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def fire_turn_hook(session_id, event="Stop"):
    payload = json.dumps({"session_id": session_id, "hook_event_name": event})
    subprocess.run([sys.executable, str(SERVER / "nth_turn_hook.py")],
                   input=payload, text=True,
                   env={**os.environ, "NTH_DB_PATH": DB}, check=True)


def turn_end(session_id):
    c = raw()
    try:
        row = c.execute("SELECT last_turn_end FROM sessions WHERE fingerprint=?",
                        (session_id,)).fetchone()
    finally:
        c.close()
    return row["last_turn_end"] if row else None


check("hook: last_turn_end starts empty", turn_end("wi-sid-1") is None)
fire_turn_hook("wi-sid-1", "Stop")
check("hook: Stop stamps last_turn_end", turn_end("wi-sid-1") is not None)
end_after_stop = turn_end("wi-sid-1")
fire_turn_hook("wi-sid-1", "StopFailure")
check("hook: StopFailure also stamps (a stalled turn still 'ends')",
      turn_end("wi-sid-1") is not None and turn_end("wi-sid-1") >= end_after_stop)
fire_turn_hook("wi-sid-nobody", "Stop")  # unknown session — must not crash
check("hook: unknown session_id is a harmless no-op",
      turn_end("wi-sid-nobody") is None)


# ── the turn hook's UPDATE must stay scoped to the live session ──────────────
# nth_connect mints a fresh member_id per connect and never revokes the old row,
# so one CLAUDE_CODE_SESSION_ID accumulates a sessions row per reconnect. An
# UPDATE keyed on `fingerprint` alone stamps every one of them, resurrecting
# long-dead members as "working" and corrupting effective_last_seen. The hook
# scopes to the newest live row per channel; this guards that it stays scoped.
# (nth_activity_hook.py carries the same scope, guarded in test-tool-activity.py.)
_c = raw()
try:
    _c.execute(
        "INSERT INTO sessions (session_token, member_id, channel, fingerprint,"
        " connected_at, last_seen, role) VALUES (?,?,?,?,?,?,?)",
        ("wi-stale-tok", "m-stale", CH, "wi-sid-1", iso(-9999), "", "member"))
finally:
    _c.close()

fire_turn_hook("wi-sid-1", "Stop")

_c = raw()
try:
    _stale = _c.execute(
        "SELECT last_turn_end FROM sessions WHERE session_token=?",
        ("wi-stale-tok",)).fetchone()
    _live = _c.execute(
        "SELECT last_turn_end FROM sessions WHERE fingerprint='wi-sid-1'"
        " AND session_token != 'wi-stale-tok' ORDER BY connected_at DESC"
        ).fetchone()
finally:
    _c.close()

check("scoping: a stale reconnect row is NOT stamped by the turn hook",
      _stale["last_turn_end"] is None)
check("scoping: the newest live row IS stamped by the turn hook",
      _live["last_turn_end"] is not None)


# ── roster integration: status flips idle -> working on new activity ─────────
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


# right now: session acted at connect (last_seen ~now), then a turn end was
# stamped just after -> last_turn_end >= activity -> idle
c = raw()
try:
    c.execute("UPDATE members SET last_seen=? WHERE channel=? AND id=?", (iso(0), CH, agent))
    c.execute("UPDATE sessions SET last_seen=?, last_turn_end=? WHERE fingerprint=?",
              (iso(-20), iso(-5), "wi-sid-1"))
finally:
    c.close()
check("roster: turn ended after last activity -> idle", status_of(agent) == "idle")

# the agent acts again (a fresh poll) AFTER the turn end -> working
c = raw()
try:
    c.execute("UPDATE sessions SET last_seen=? WHERE fingerprint=?", (iso(0), "wi-sid-1"))
finally:
    c.close()
check("roster: activity after turn end -> working", status_of(agent) == "working")

# a member with no turn data recorded shows the legacy 'active' (no regression)
os.environ["CLAUDE_CODE_SESSION_ID"] = "wi-sid-legacy"
r2 = json.loads(srv.nth_connect(summary="t", name="Legacyish", channel=CH))
legacy = r2["member_id"]
c = raw()
try:
    c.execute("UPDATE members SET last_seen=? WHERE channel=? AND id=?", (iso(0), CH, legacy))
    c.execute("UPDATE sessions SET last_seen=?, last_turn_end=NULL WHERE fingerprint=?",
              (iso(0), "wi-sid-legacy"))
finally:
    c.close()
check("roster: no turn data -> legacy 'active' (no regression)", status_of(legacy) == "active")



# ── Threshold semantics ─────────────────────────────────────────────────────
# These pin the > vs >= choice at each boundary. Without them a mutation from
# `>` to `>=` on either threshold, or on the working/idle split, passes silently.
from datetime import datetime, timezone, timedelta
import nth_web as _w

def _iso_ago(sec):
    return (datetime.now(timezone.utc) - timedelta(seconds=sec)).isoformat()

# NB: exact equality (age == STALE_SECONDS) cannot be asserted without freezing
# the clock — microseconds elapse between building the timestamp and reading it,
# so "exactly N seconds ago" always evaluates fractionally over N. These pin the
# threshold's location and direction, which is what a regression would move.
check("boundary: just under STALE_SECONDS is still active",
      _w.member_status(_iso_ago(_w.STALE_SECONDS - 5), "") == "active")
check("boundary: just over STALE_SECONDS is stale",
      _w.member_status(_iso_ago(_w.STALE_SECONDS + 5), "") == "stale")
check("boundary: just under DEAD_SECONDS is stale, not dead",
      _w.member_status(_iso_ago(_w.DEAD_SECONDS - 5), "") == "stale")
check("boundary: just over DEAD_SECONDS is dead",
      _w.member_status(_iso_ago(_w.DEAD_SECONDS + 5), "") == "dead")

# working/idle split: activity must be strictly AFTER the turn end. Equal
# timestamps are plausible at second resolution and must read idle, not working.
_now = _iso_ago(1)
check("boundary: activity == turn end reads idle (strict >)",
      _w.member_status(_iso_ago(1), "", session_activity_iso=_now,
                       last_turn_end_iso=_now) == "idle")
check("boundary: activity after turn end reads working",
      _w.member_status(_iso_ago(1), "", session_activity_iso=_iso_ago(1),
                       last_turn_end_iso=_iso_ago(30)) == "working")

# ── Malformed / adversarial timestamps degrade, never raise ─────────────────
for bad in (None, "", "   ", "not-a-timestamp"):
    check(f"malformed last_seen {bad!r} -> dead (no raise)",
          _w.member_status(bad, "") == "dead")
check("malformed last_turn_end falls back to legacy 'active'",
      _w.member_status(_iso_ago(1), "", session_activity_iso=_iso_ago(1),
                       last_turn_end_iso="garbage") == "active")
check("clock skew: last_seen in the future does not crash",
      _w.member_status(
          (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(), "")
      in {"active", "working", "idle"})

os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
