#!/usr/bin/env python3
"""Durable idle-channel archive policy and resurface regression."""
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import nth_web as web
from nth_constants import AUTO_ARCHIVE_BY, AUTO_ARCHIVE_RESURFACE_TRIGGER_SQL

failures = 0
def check(ok, label):
    global failures
    print(("PASS" if ok else "FAIL") + ": " + label)
    failures += not ok

with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "nth.db"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE channels(code TEXT PRIMARY KEY,status TEXT,created_at TEXT,updated_at TEXT,
        archived_at TEXT,archived_by TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel TEXT,member_id TEXT,
        content TEXT,created_at TEXT,choices TEXT DEFAULT '',reply_to INTEGER);
      CREATE TABLE members(id TEXT,channel TEXT,kind TEXT,active INTEGER,last_seen TEXT);
      CREATE TABLE message_reads(message_id INTEGER,member_id TEXT);
      CREATE TABLE tasks(channel TEXT,status TEXT);
      CREATE TABLE locks(channel TEXT,expires_at TEXT);
    """)
    db.execute(AUTO_ARCHIVE_RESURFACE_TRIGGER_SQL)
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    old = (now - timedelta(days=2)).isoformat()
    edge = (now - timedelta(days=1)).isoformat()
    for code, created in (("idle", old), ("boundary", edge), ("manual", old)):
        db.execute("INSERT INTO channels VALUES(?,?,?,?,NULL,NULL)",
                   (code, "active", created, created))
    db.execute("UPDATE channels SET archived_at=?,archived_by='human' WHERE code='manual'", (old,))
    db.commit(); db.close()

    archived = web.auto_archive_inactive_channels(path, now=now)
    check(archived == ["idle"], "only activity strictly older than 24h auto-archives")
    db = sqlite3.connect(path)
    check(db.execute("SELECT archived_by FROM channels WHERE code='idle'").fetchone()[0] == AUTO_ARCHIVE_BY,
          "auto archive records system provenance")
    db.execute("INSERT INTO messages(channel,member_id,content,created_at) VALUES('idle','a','wake',?)",
               (now.isoformat(),))
    db.execute("INSERT INTO messages(channel,member_id,content,created_at) VALUES('manual','a','stay',?)",
               (now.isoformat(),))
    db.commit()
    check(db.execute("SELECT archived_at FROM channels WHERE code='idle'").fetchone()[0] is None,
          "new activity resurfaces an automatic archive")
    check(db.execute("SELECT archived_at FROM channels WHERE code='manual'").fetchone()[0] is not None,
          "new activity does not undo a manual archive")
    db.close()

print(f"\n{'OK' if not failures else 'FAILED'} — {failures} failure(s)")
raise SystemExit(1 if failures else 0)
