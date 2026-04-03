"""
Background polling script for Claude Trio.

Launched via Bash with run_in_background=true. Polls the SQLite database
until new messages arrive from other members, then prints the result
and exits. Claude gets a task-notification when this completes.

v4: This script is notification-only — it NEVER advances the read
watermark in the database. Watermark advancement is the responsibility
of trio_poll (auto-advance) or trio_ack (explicit). This eliminates
the watermark race condition where trio_wait and trio_poll both
advanced last_read independently, causing silent message loss.

The script tracks its own last-seen ID in a local variable to avoid
re-reporting the same messages across poll cycles within a single run.

Usage:
    python trio_wait.py <channel> <member_id> [--timeout SECONDS]

Output on completion (JSON, one line):
    {"event": "new_messages", "messages": [{...}, ...]}
    {"event": "ended", "ended_by": "..."}
    {"event": "channel_gone"}
    {"event": "timeout"}
"""

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "trio" / "trio.db"
POLL_INTERVAL = 3  # seconds between checks


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


DEFAULT_TIMEOUT = 300  # 5 minutes — exit cleanly before Bash kills us


def poll_for_messages(channel, member_id, timeout=DEFAULT_TIMEOUT):
    deadline = time.time() + timeout
    # Local high-water mark — tracks which messages THIS run has already
    # seen, without touching the DB watermark. Initialized from the DB
    # on first iteration, then advanced locally as messages arrive.
    local_hwm = None

    while time.time() < deadline:
        db = get_db()
        try:
            # Update heartbeat only — never advance last_read
            try:
                db.execute(
                    "UPDATE members SET last_seen = ? WHERE channel = ? AND id = ?",
                    (now_iso(), channel, member_id),
                )
                db.commit()
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    return {"error": "Database not initialized. Run trio_connect first to create the schema."}
                raise

            # Check channel status
            ch = db.execute(
                "SELECT status, ended_by FROM channels WHERE code = ?",
                (channel,),
            ).fetchone()

            if not ch:
                return {"event": "channel_gone"}

            if ch["status"] == "ended":
                member = db.execute(
                    "SELECT last_read FROM members WHERE channel = ? AND id = ?",
                    (channel, member_id),
                ).fetchone()
                last_read = member["last_read"] if member else 0
                unread = db.execute(
                    "SELECT id, member_id, member_name, content, created_at "
                    "FROM messages WHERE channel = ? AND id > ? ORDER BY id",
                    (channel, last_read),
                ).fetchall()
                ended_by_name = ch["ended_by"]
                if ch["ended_by"]:
                    ender = db.execute(
                        "SELECT name FROM members WHERE channel = ? AND id = ?",
                        (channel, ch["ended_by"]),
                    ).fetchone()
                    if ender:
                        ended_by_name = ender["name"]
                return {
                    "event": "ended",
                    "ended_by": ended_by_name,
                    "unread": [
                        {"id": m["id"], "from": m["member_name"] or m["member_id"],
                         "content": m["content"], "at": m["created_at"]}
                        for m in unread
                    ],
                }

            # Initialize local high-water mark from DB on first pass
            if local_hwm is None:
                member = db.execute(
                    "SELECT last_read FROM members WHERE channel = ? AND id = ?",
                    (channel, member_id),
                ).fetchone()
                if not member:
                    return {"event": "channel_gone"}
                local_hwm = member["last_read"]

            # Check for messages beyond our local high-water mark
            unread = db.execute(
                "SELECT id, member_id, member_name, content, created_at "
                "FROM messages WHERE channel = ? AND id > ? AND member_id != ? ORDER BY id",
                (channel, local_hwm, member_id),
            ).fetchall()

            if unread:
                # Advance LOCAL high-water mark only — DB watermark untouched.
                # trio_poll or trio_ack will advance the DB watermark when
                # the agent processes these messages through MCP.
                local_hwm = max(m["id"] for m in unread)

                return {
                    "event": "new_messages",
                    "messages": [
                        {"id": m["id"], "from": m["member_name"] or m["member_id"],
                         "content": m["content"], "at": m["created_at"]}
                        for m in unread
                    ],
                }
        finally:
            db.close()

        time.sleep(POLL_INTERVAL)

    return {"event": "timeout"}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: trio_wait.py <channel> <member_id> [--timeout SECONDS]"}))
        sys.exit(1)

    channel = sys.argv[1]
    member_id = sys.argv[2]
    timeout = DEFAULT_TIMEOUT
    if "--timeout" in sys.argv:
        idx = sys.argv.index("--timeout")
        if idx + 1 < len(sys.argv):
            try:
                timeout = max(1, int(sys.argv[idx + 1]))
            except ValueError:
                pass
    result = poll_for_messages(channel, member_id, timeout=timeout)
    print(json.dumps(result))
