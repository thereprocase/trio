"""Regression test: on a schema missing the refs/bangs columns (pre-v7.1/7.2),
the monitor's fallback SELECTs must still read recipients + member_id so a DM
addressed to someone else is not treated as a visible broadcast.

bugs/2026-08-01-old-schema-monitor-leaks-dm-preview.md: the fallback queries
omitted recipients/member_id entirely, so can_see() saw an empty recipient
list and admitted every row as a broadcast — leaking another member's private
DM content into this member's wake preview.

Usage: python test-monitor-old-schema-dm-leak.py
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import nth_monitor as nm  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_old_schema_db(path):
    """messages table WITHOUT refs/bangs — the pre-v7.1 shape — but WITH the
    foundational recipients/member_id columns every real DB has always had."""
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE channels (code TEXT PRIMARY KEY, status TEXT, ended_by TEXT);
        CREATE TABLE members (
            id TEXT, channel TEXT, name TEXT, last_seen TEXT,
            last_read INTEGER DEFAULT 0, status_text TEXT DEFAULT '',
            messenger_heartbeat TEXT DEFAULT '', watchdog_heartbeat TEXT DEFAULT '',
            filter_mode TEXT, PRIMARY KEY (id, channel)
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, member_id TEXT,
            member_name TEXT, content TEXT, created_at TEXT,
            mentions TEXT DEFAULT '', recipients TEXT DEFAULT ''
        );
        CREATE TABLE sessions (
            channel TEXT, member_id TEXT, last_read INTEGER DEFAULT 0, revoked_at TEXT
        );
        CREATE TABLE tasks (channel TEXT, claimed_by TEXT, status TEXT);
        """
    )
    db.execute("INSERT INTO channels (code, status) VALUES ('CHAN', 'active')")
    db.execute(
        "INSERT INTO members (id, channel, name, filter_mode) VALUES "
        "('m1', 'CHAN', 'Alice', 'all'), ('m3', 'CHAN', 'Carol', 'all')"
    )
    db.commit()
    db.close()


def insert_dm_not_for_m1(path, content):
    """A private DM from m2 to m3 ONLY — m1 must never see this content."""
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA busy_timeout=5000")
    db.execute(
        "INSERT INTO messages (channel, member_id, member_name, content, "
        "created_at, recipients) VALUES ('CHAN', 'm2', 'Bob', ?, ?, ?)",
        (content, now_iso(), json.dumps(["m3"])),
    )
    db.commit()
    db.close()


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


def run_monitor(db_path, member_id):
    cap = Capture()
    nm.emit = cap
    t = threading.Thread(
        target=nm.monitor,
        kwargs={"channel": "CHAN", "member_id": member_id,
                "filter_mode": "all", "_db_path": str(db_path)},
        daemon=True,
    )
    t.start()
    return cap, t


def wait_until(pred, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


tmpdir = Path(tempfile.mkdtemp(prefix="nth-old-schema-dm-leak-"))
db_path = tmpdir / "old.db"
build_old_schema_db(db_path)
secret_content = "TOP SECRET for Carol only"
insert_dm_not_for_m1(db_path, secret_content)

cap, t = run_monitor(db_path, member_id="m1")
# Give the monitor a couple of poll cycles to observe the row and (if the bug
# were present) emit a leaking new_messages event. The thread is a daemon, so
# it dies with the process — no explicit stop needed.
time.sleep(1.5)
events = cap.snapshot()
leaked = [e for e in events if secret_content in json.dumps(e)]
check("old-schema fallback does not leak a DM preview to a non-recipient",
      not leaked)
check("old-schema fallback does not wake a non-recipient on someone else's DM",
      not any(e.get("event") == "new_messages" for e in events))

print(f"\n{'OK' if not failures else 'FAILED'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
