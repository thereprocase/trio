"""Regression test: the monitor must EXIT when its member is culled.

Reproduces Bug B2 (FUTURE_IMPROVEMENTS.md): an operator cull hard-DELETEs the
member row and revokes its sessions, but the monitor historically treated a
missing member row as a TRANSIENT error — emit {"event":"error", ...}, then
sleep(10); continue — so it looped forever and the culled agent effectively
stayed in the channel.

The fix tracks member_seen: once the members SELECT returns a row, a later
absence is a cull, not the startup join race. On absence-after-presence the
monitor emits a dedicated terminal {"event":"culled", ...} and returns cleanly,
exactly like channel_ended. A never-yet-seen row at startup keeps the lenient
retry (error + sleep + continue) so the monitor tolerates the join race.

This test drives the REAL monitor() loop against a temporary sqlite DB — it
exercises the actual production exit path, not duplicated logic.

Usage: python test-monitor-culled-exit.py
"""
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Import the real module under test from ../server.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import nth_monitor as nm


failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_db(path, with_member=True):
    """Minimal schema covering exactly the columns monitor() queries."""
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE channels (code TEXT PRIMARY KEY, status TEXT, ended_by TEXT);
        CREATE TABLE members (
            id TEXT, channel TEXT, name TEXT, last_seen TEXT,
            last_read INTEGER DEFAULT 0, status_text TEXT DEFAULT '',
            messenger_heartbeat TEXT DEFAULT '', watchdog_heartbeat TEXT DEFAULT '',
            filter_mode TEXT DEFAULT 'all', PRIMARY KEY (id, channel)
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, member_id TEXT,
            member_name TEXT, content TEXT, created_at TEXT,
            mentions TEXT DEFAULT '', refs TEXT DEFAULT '', bangs TEXT DEFAULT ''
        );
        CREATE TABLE sessions (
            channel TEXT, member_id TEXT, last_read INTEGER DEFAULT 0, revoked_at TEXT
        );
        CREATE TABLE tasks (channel TEXT, claimed_by TEXT, status TEXT);
        """
    )
    db.execute("INSERT INTO channels (code, status) VALUES ('CHAN', 'active')")
    if with_member:
        db.execute(
            "INSERT INTO members (id, channel, name) VALUES ('m1', 'CHAN', 'Alice')"
        )
    db.commit()
    db.close()


# Thread-safe capture of everything the monitor emits.
class Capture:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event_dict):
        with self.lock:
            self.events.append(event_dict)

    def snapshot(self):
        with self.lock:
            return list(self.events)


def run_monitor(db_path):
    cap = Capture()
    orig_emit = nm.emit
    nm.emit = cap  # monitor() calls the module-level emit()
    t = threading.Thread(
        target=nm.monitor,
        kwargs={"channel": "CHAN", "member_id": "m1", "_db_path": str(db_path)},
        daemon=True,
    )
    t.start()
    return cap, t, orig_emit


def wait_until(pred, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


tmpdir = Path(tempfile.mkdtemp(prefix="nth-culled-test-"))

# ---------------------------------------------------------------------------
# Case 1: member present, then culled (row deleted) -> terminal `culled` + exit.
# ---------------------------------------------------------------------------
db_path = tmpdir / "present.db"
build_db(db_path, with_member=True)
cap, t, orig_emit = run_monitor(db_path)
try:
    # The monitor stamps messenger_heartbeat on its first tick once it has
    # seen the member row present — that write is our proof member_seen flipped.
    def heartbeat_written():
        c = sqlite3.connect(str(db_path))
        try:
            row = c.execute(
                "SELECT messenger_heartbeat FROM members WHERE id='m1' AND channel='CHAN'"
            ).fetchone()
            return bool(row and row[0])
        finally:
            c.close()

    seen = wait_until(heartbeat_written, timeout=5.0)
    check("monitor observed the member present (heartbeat written)", seen)

    # Cull: hard-DELETE the member row, exactly as the server does.
    c = sqlite3.connect(str(db_path))
    c.execute("DELETE FROM members WHERE id='m1' AND channel='CHAN'")
    c.commit()
    c.close()

    # The monitor should notice within one poll interval, emit `culled`, return.
    exited = wait_until(lambda: not t.is_alive(), timeout=5.0)
    check("monitor thread exited after cull", exited)

    evs = cap.snapshot()
    culled = [e for e in evs if e.get("event") == "culled"]
    check("emitted exactly one `culled` event", len(culled) == 1)
    if culled:
        e = culled[0]
        check("culled event carries member_id", e.get("member_id") == "m1")
        check("culled event carries channel", e.get("channel") == "CHAN")
    # The bug was emitting a recoverable `error` for a cull — must NOT happen
    # once the member has been seen present.
    check("no `error` event emitted for a seen-then-culled member",
          not any(e.get("event") == "error" for e in evs))
finally:
    nm.emit = orig_emit

# ---------------------------------------------------------------------------
# Case 2: member never present at startup (join race) -> lenient `error`, no exit.
# ---------------------------------------------------------------------------
db_path2 = tmpdir / "absent.db"
build_db(db_path2, with_member=False)
cap2, t2, orig_emit2 = run_monitor(db_path2)
try:
    got_error = wait_until(
        lambda: any(e.get("event") == "error" for e in cap2.snapshot()),
        timeout=3.0,
    )
    check("never-seen member yields a lenient `error` event", got_error)
    check("never-seen member does NOT yield a `culled` event",
          not any(e.get("event") == "culled" for e in cap2.snapshot()))
    # It must keep running (the row could still appear — the join race).
    check("monitor stays alive for a never-seen member", t2.is_alive())
finally:
    nm.emit = orig_emit2
    # t2 is a daemon thread mid-sleep(10); the process exit reaps it.

if failures:
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("\nAll checks passed.")
