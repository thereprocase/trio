"""A turn that dies to an API error shows STALLED on the roster.

A Claude turn killed by a transient API error (a 529 overload, a rate limit)
does not retry itself: the session freezes mid-work and goes quiet. Nothing
notices, because the member's Monitor keeps heartbeating members.last_seen
while the session it watches is dead — so a frozen agent reads as healthy.

Two halves, both driven for real here:

  * nth_stall_hook.py records one stall_events row per dead turn. Run as an
    actual subprocess with a real payload on stdin, because the thing that
    bit the first version of this feature was the difference between the
    vocabulary the tests used and the vocabulary Claude Code emits.
  * EventHub._fetch_roster derives the badge from those rows. It is a BADGE,
    not an actuator — nothing here spends a token or starts a turn.

Usage: python tests/test-stall-badge.py
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

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

_tmp = Path(tempfile.mkdtemp(prefix="nth_stall_badge_"))
DB = _tmp / "nth.db"
os.environ["NTH_DB_PATH"] = str(DB)

import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

srv.DB_DIR = _tmp
srv.DB_PATH = DB

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fire_hook(session_id, error, db_path=DB, cwd="/tmp"):
    """Run the REAL hook as a subprocess, the way Claude Code does."""
    payload = {"hook_event_name": "StopFailure", "session_id": session_id,
               "error": error, "cwd": cwd}
    env = dict(os.environ, NTH_DB_PATH=str(db_path))
    proc = subprocess.run(
        [sys.executable, str(SERVER / "nth_stall_hook.py")],
        input=json.dumps(payload), text=True, capture_output=True,
        env=env, timeout=20)
    return proc.returncode


def rows(db_path=DB):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM stall_events ORDER BY id").fetchall()]
    finally:
        c.close()


def roster_for(channel):
    hub = web.EventHub(DB, channel)
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    try:
        return {m["id"]: m for m in hub._fetch_roster(c)}
    finally:
        c.close()


# ── 1. The hook records, on a database that has never seen the table ────────
# The DDL self-heal exists for exactly one case: firing on a fresh install
# before any server process has created the table. Drive it against a
# genuinely empty database, or the path never runs.
fresh = _tmp / "fresh.db"
sqlite3.connect(str(fresh)).close()
check("hook exits 0 against a database with no stall_events table",
      fire_hook("sid-fresh", "overloaded_error", db_path=fresh) == 0)
check("hook self-healed the schema and recorded the stall",
      len(rows(fresh)) == 1 and rows(fresh)[0]["session_id"] == "sid-fresh")

# The error text is stored verbatim. The first version of this feature matched
# the literal string "overloaded" while the API reports "overloaded_error",
# so a real 529 — the flagship case — was misclassified. Whatever consumes
# these rows must see what Claude Code actually emits, not a normalised stem.
check("hook stores the error verbatim, not normalised",
      rows(fresh)[0]["error"] == "overloaded_error")

# ── 2. A frozen session shows STALLED on the roster ─────────────────────────
r = json.loads(srv.nth_connect(summary="t", name="Frozen", channel="stallroom"))
CH, MEMBER = r["channel"], r["member_id"]

c = sqlite3.connect(str(DB))
try:
    fp = c.execute("SELECT fingerprint FROM sessions WHERE member_id=?",
                   (MEMBER,)).fetchone()
finally:
    c.close()
check("the session has a fingerprint to map back to", fp is not None and fp[0])
FP = fp[0] if fp else ""

check("roster is clean before any stall", roster_for(CH)[MEMBER]["stalled"] is None)

fire_hook(FP, "overloaded_error")
badge = roster_for(CH)[MEMBER]["stalled"]
check("a dead turn puts the member in STALLED", badge is not None)
check("the badge carries the error so a human can judge it",
      bool(badge) and badge.get("error") == "overloaded_error")
check("the badge carries when it happened", bool(badge) and bool(badge.get("since")))

# ── 3. A session that resumes on its own stops being stalled ────────────────
# Resume is detected from sessions.last_seen — the session's OWN tool activity.
# members.last_seen is deliberately not consulted: the Monitor keeps it ticking
# while the session is frozen, which is the whole reason this badge exists.
c = sqlite3.connect(str(DB))
try:
    c.execute("UPDATE members SET last_seen=? WHERE id=?", (now_iso(), MEMBER))
    c.commit()
finally:
    c.close()
check("a Monitor heartbeat alone does NOT clear the badge",
      roster_for(CH)[MEMBER]["stalled"] is not None)

c = sqlite3.connect(str(DB))
try:
    c.execute("UPDATE sessions SET last_seen=? WHERE member_id=?",
              (now_iso(), MEMBER))
    c.commit()
finally:
    c.close()
check("the session's own activity clears the badge",
      roster_for(CH)[MEMBER]["stalled"] is None)

# ── 4. A resolved event never re-badges ─────────────────────────────────────
c = sqlite3.connect(str(DB))
try:
    c.execute("UPDATE stall_events SET resolved_at=?, resolution='resumed'",
              (now_iso(),))
    # Roll the session's activity back BEFORE the stall, so the only reason
    # the badge stays away is the resolution — not a fresh last_seen.
    c.execute("UPDATE sessions SET last_seen='2000-01-01T00:00:00+00:00' "
              "WHERE member_id=?", (MEMBER,))
    c.commit()
finally:
    c.close()
check("a resolved stall does not come back", roster_for(CH)[MEMBER]["stalled"] is None)

# ── 5. Retention: nothing else reclaims these rows ──────────────────────────
# The consumer that used to prune them is not in this branch, and a spoke
# install has no hub process at all — so the hook prunes on write, or the
# table grows one row per dead turn forever.
import nth_stall_hook as hook   # noqa: E402
check("a retention window is defined", hook.STALL_RETENTION_HOURS > 0)

old = (datetime.now(timezone.utc)
       - timedelta(hours=hook.STALL_RETENTION_HOURS + 24)).isoformat()
recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
c = sqlite3.connect(str(DB))
try:
    c.execute("DELETE FROM stall_events")
    c.execute("INSERT INTO stall_events (session_id, error, cwd, created_at) "
              "VALUES ('old-sid','x','',?)", (old,))
    c.execute("INSERT INTO stall_events (session_id, error, cwd, created_at) "
              "VALUES ('recent-sid','x','',?)", (recent,))
    c.commit()
finally:
    c.close()
fire_hook("sid-prune-trigger", "rate_limit")
kept = {row["session_id"] for row in rows()}
check("rows past the retention window are pruned", "old-sid" not in kept)
check("rows inside it are kept", "recent-sid" in kept)
check("the pruning write did not cost us the new row", "sid-prune-trigger" in kept)

# The retention bound is pinned to an absolute range rather than derived from
# the constant it guards — deriving it would prove only "the code equals
# itself", which is how the previous version of this suite stayed green with
# the value set to something absurd.
check("retention is at least a day (a human may not look until tomorrow)",
      hook.STALL_RETENTION_HOURS >= 24)
check("retention is not unbounded (weeks of dead rows help nobody)",
      hook.STALL_RETENTION_HOURS <= 24 * 14)

# ── 6. The roster must not break on a database without the table ────────────
noschema = _tmp / "noschema.db"
c = sqlite3.connect(str(noschema))
try:
    c.executescript("""
        CREATE TABLE channels (code TEXT PRIMARY KEY, status TEXT, ended_by TEXT);
        CREATE TABLE members (id TEXT, channel TEXT, name TEXT, last_seen TEXT,
            last_read INTEGER DEFAULT 0, status_text TEXT DEFAULT '',
            PRIMARY KEY (id, channel));
        CREATE TABLE sessions (channel TEXT, member_id TEXT,
            last_read INTEGER DEFAULT 0, last_seen TEXT, revoked_at TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT);
    """)
    c.execute("INSERT INTO channels (code,status) VALUES ('old','active')")
    c.execute("INSERT INTO members (id,channel,name) VALUES ('m1','old','Alice')")
    c.commit()
finally:
    c.close()
hub_old = web.EventHub(noschema, "old")
c = sqlite3.connect(str(noschema))
c.row_factory = sqlite3.Row
try:
    ok = True
    try:
        got = hub_old._fetch_roster(c)
    except Exception as e:      # noqa: BLE001
        ok, got = False, repr(e)
finally:
    c.close()
check("a roster on a pre-stall-hook schema still renders", ok is True)
check("and simply reports no badges",
      ok and all(m.get("stalled") is None for m in got))

shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
