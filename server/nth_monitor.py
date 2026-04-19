"""
Persistent event monitor for nth channels.

Replaces the Haiku-subagent messenger+watchdog pair with a single
long-running Python script. Intended to be invoked via Claude Code's
`Monitor` tool with `persistent=True`:

    Monitor(
        command=f"python3 .../nth_monitor.py {channel} {member_id} --mention-filter",
        description=f"{channel} events",
        persistent=True,
        timeout_ms=3600000,  # ignored at runtime when persistent=True, but schema requires >=1000
    )

Windows: substitute `py` for `python3` (PEP 397 launcher). The script
itself is pure stdlib (sqlite3, pathlib, json) and runs on any OS.

Each line emitted to stdout is a separate notification to the parent
Claude. The script never exits on its own unless the channel ends.

Events (one JSON line per fire):
    {"event": "new_messages", "message_ids": [...], "count": N, "mode": "active"|"idle"}
    {"event": "cadence", "gap_seconds": N}
    {"event": "channel_ended", "ended_by": "..."}
    {"event": "channel_gone"}
    {"event": "error", "msg": "..."}

Mention filter: when --mention-filter is passed, new_messages only
fires when the message's `mentions` JSON array is empty (broadcast)
or contains this member_id. Other messages still advance the
watermark so they are never re-surfaced.

Cadence: fires once per silence period when the member is in active
mode (no sleeping keyword in status_text) and has not posted for
CADENCE_THRESHOLD seconds. Resets when the member posts again.
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nth_constants import SLEEPING_KEYWORDS

DB_PATH = Path.home() / ".claude" / "nth" / "nth.db"

ACTIVE_INTERVAL = 3
IDLE_INTERVAL = 30
CADENCE_THRESHOLD = 600


def emit(event_dict):
    print(json.dumps(event_dict), flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def seconds_since(iso_timestamp):
    if not iso_timestamp:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def is_sleeping(status_text):
    if not status_text:
        return False
    lower = status_text.lower()
    return any(kw in lower for kw in SLEEPING_KEYWORDS)


def monitor(channel, member_id, mention_filter=False, _db_path=None):
    local_hwm = None
    cadence_fired = False
    db_error_streak = 0

    db_path = _db_path or DB_PATH
    db = sqlite3.connect(str(db_path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")

    try:
        while True:
            try:
                member = db.execute(
                    "SELECT last_seen, last_read, status_text "
                    "FROM members WHERE channel = ? AND id = ?",
                    (channel, member_id),
                ).fetchone()

                if not member:
                    emit({"event": "error", "msg": "Member not found in channel."})
                    time.sleep(10)
                    continue

                ch = db.execute(
                    "SELECT status, ended_by FROM channels WHERE code = ?",
                    (channel,),
                ).fetchone()

                if not ch:
                    emit({"event": "channel_gone"})
                    return

                if ch["status"] == "ended":
                    ender_name = ch["ended_by"]
                    if ch["ended_by"]:
                        ender = db.execute(
                            "SELECT name FROM members WHERE channel = ? AND id = ?",
                            (channel, ch["ended_by"]),
                        ).fetchone()
                        if ender:
                            ender_name = ender["name"]
                    emit({"event": "channel_ended", "ended_by": ender_name})
                    return

                now_ts = now_iso()
                db.execute(
                    "UPDATE members SET last_seen = ? WHERE channel = ? AND id = ?",
                    (now_ts, channel, member_id),
                )
                db.commit()

                sleeping = is_sleeping(member["status_text"])
                check_interval = IDLE_INTERVAL if sleeping else ACTIVE_INTERVAL

                # --- New messages ---
                if local_hwm is None:
                    legacy_hwm = member["last_read"] or 0
                    try:
                        sess_row = db.execute(
                            "SELECT MAX(last_read) AS hwm FROM sessions "
                            "WHERE channel = ? AND member_id = ? "
                            "AND revoked_at IS NULL AND role = 'primary'",
                            (channel, member_id),
                        ).fetchone()
                        sess_hwm = (sess_row["hwm"] or 0) if sess_row else 0
                    except sqlite3.OperationalError:
                        sess_hwm = 0
                    local_hwm = max(legacy_hwm, sess_hwm)

                unread = db.execute(
                    "SELECT id, mentions FROM messages "
                    "WHERE channel = ? AND id > ? AND member_id != ? "
                    "ORDER BY id",
                    (channel, local_hwm, member_id),
                ).fetchall()

                if unread:
                    local_hwm = max(m["id"] for m in unread)

                    if mention_filter:
                        relevant = []
                        for m in unread:
                            raw = m["mentions"] if "mentions" in m.keys() else ""
                            if not raw:
                                relevant.append(m)
                                continue
                            try:
                                ids = json.loads(raw)
                            except (ValueError, TypeError):
                                ids = []
                            if member_id in ids:
                                relevant.append(m)
                    else:
                        relevant = list(unread)

                    if relevant:
                        emit({
                            "event": "new_messages",
                            "mode": "idle" if sleeping else "active",
                            "message_ids": [m["id"] for m in relevant],
                            "count": len(relevant),
                        })

                # --- Cadence (active mode only, fire-once per silence period) ---
                if not sleeping:
                    latest_own = db.execute(
                        "SELECT created_at FROM messages "
                        "WHERE channel = ? AND member_id = ? ORDER BY id DESC LIMIT 1",
                        (channel, member_id),
                    ).fetchone()
                    gap = seconds_since(
                        latest_own["created_at"] if latest_own else None
                    )
                    if gap > CADENCE_THRESHOLD and not cadence_fired:
                        emit({"event": "cadence", "gap_seconds": round(gap)})
                        cadence_fired = True
                    elif gap < CADENCE_THRESHOLD:
                        cadence_fired = False
                else:
                    cadence_fired = False

            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    emit({"event": "error", "msg": "Database not initialized."})
                    return
                db_error_streak += 1
                if db_error_streak >= 10:
                    emit({"event": "error", "msg": f"Persistent DB failure: {e}"})
                    db_error_streak = 0
            else:
                db_error_streak = 0

            time.sleep(check_interval)

    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        emit({"event": "error",
              "msg": "Usage: nth_monitor.py <channel> <member_id> [--mention-filter]"})
        sys.exit(1)

    channel_arg = sys.argv[1]
    member_arg = sys.argv[2]
    mention_filter_arg = "--mention-filter" in sys.argv[3:]

    try:
        monitor(channel_arg, member_arg, mention_filter=mention_filter_arg)
    except KeyboardInterrupt:
        pass
