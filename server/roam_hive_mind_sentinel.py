"""
Unified monitoring sentinel for Trio agents (v5).

Replaces both roam_hive_mind_wait.py (message detection) and
roam_hive_mind_watchdog.py (heartbeat/cadence monitoring) with a single
adaptive process. One script, one agent, all monitoring concerns.

MODES (auto-detected from member's status_text in SQLite):
  - ACTIVE: 3s check interval. Watches messages, cadence, peer heartbeat.
  - IDLE: 30s interval. Watches messages, flag consistency, peer heartbeat.
  - SLEEP: 30s interval. Peer heartbeat only (after 60s confirmation).

The sentinel writes heartbeat updates (last_seen + role-specific column)
and reads state to report events to the parent agent.

RETURNS to parent only when something needs attention:
  - new_messages: real messages from other members
  - cadence: too long without a status post (active mode only)
  - flag_inconsistency: sleeping flag but actively sending messages
  - peer_dead: the other sentinel's heartbeat is stale (5 min, 2 checks)
  - channel_ended: channel was ended by another member
  - channel_gone: channel row deleted from DB
  - error: persistent DB failure or invalid state
  - cap: max runtime exceeded — wrapper converts to restart event

Usage:
    python roam_hive_mind_sentinel.py <channel> <member_id> [options]

Options:
    --max-runtime SECONDS       (default: 3540 — 59 min, from roam_constants.py)
    --heartbeat-threshold SECONDS   (default: 120 — active mode)
    --idle-heartbeat-threshold SECONDS  (default: 300 — sleep mode)
    --cadence-threshold SECONDS (default: 180 — active mode only)
    --sleep-confirm SECONDS     (default: 60 — silence before sleep)
    --active-interval SECONDS   (default: 3)
    --idle-interval SECONDS     (default: 30)
    --watch EVENTS              (default: all — comma-separated list of events
                                 to return on. Others loop internally.
                                 e.g. --watch new_messages,channel_ended)
"""

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "roam" / "roam.db"

# Defaults
DEFAULT_HEARTBEAT_THRESHOLD = 120   # 2 min (active)
DEFAULT_IDLE_HEARTBEAT_THRESHOLD = 300  # 5 min (sleep)
DEFAULT_CADENCE_THRESHOLD = 180     # 3 min (active only)
DEFAULT_SLEEP_CONFIRM = 60          # 60s silence to confirm sleep
DEFAULT_ACTIVE_INTERVAL = 3         # seconds between checks (active)
DEFAULT_IDLE_INTERVAL = 30          # seconds between checks (idle/sleep)

from roam_constants import SLEEPING_KEYWORDS, MAX_RUNTIME_S


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def seconds_since(iso_timestamp):
    """Seconds elapsed since an ISO 8601 timestamp."""
    if not iso_timestamp:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def is_sleeping_flag(status_text):
    """Check if status_text contains sleeping keywords."""
    if not status_text:
        return False
    lower = status_text.lower()
    return any(kw in lower for kw in SLEEPING_KEYWORDS)


def sentinel(channel, member_id, max_runtime, heartbeat_threshold,
             idle_heartbeat_threshold, cadence_threshold, sleep_confirm,
             active_interval, idle_interval, watch_events=None, role=None,
             _db_path=None):
    """Main sentinel loop. Returns a JSON-serializable dict on exit.

    If watch_events is set (e.g. ["new_messages", "channel_ended"]),
    the sentinel only returns for those event types and loops internally
    on all others. This prevents the Haiku agent from burning its run
    cap on events it doesn't care about.

    If role is set ("messenger" or "watchdog"), the sentinel writes its
    own heartbeat column and monitors the peer's. Returns peer_dead if
    the peer heartbeat goes stale (5 min, 2 consecutive observations).

    _db_path: override DB path for testing. Production uses DB_PATH.
    """

    PEER_DEAD_THRESHOLD = 300   # 5 minutes
    PEER_DEAD_STARTUP_GRACE = 60  # ignore peer heartbeat for first 60s
    PEER_ROLES = {"messenger": "watchdog", "watchdog": "messenger"}
    VALID_ROLES = {"messenger", "watchdog"}

    if role and role not in VALID_ROLES:
        return {"event": "error", "msg": f"Invalid role: {role}"}

    own_col = f"{role}_heartbeat" if role else None
    peer_col = f"{PEER_ROLES[role]}_heartbeat" if role and role in PEER_ROLES else None

    deadline = time.time() + max_runtime

    def should_return(event_type):
        """Check if this event type is in the watch list (if set)."""
        if watch_events is None:
            return True  # no filter — return on everything
        return event_type in watch_events

    # Persistent state across check cycles
    prev_msg_count = None
    local_hwm = None            # local high-water mark for message detection
    sleep_confirmed = False
    inconsistency_streak = 0    # consecutive checks with flag inconsistency
    peer_dead_streak = 0        # consecutive checks with stale peer heartbeat
    db_error_streak = 0         # consecutive DB errors (surfaces persistent failures)
    start_time = time.time()    # for startup grace period on peer detection

    db = None
    try:
        # Single long-lived DB connection (WAL mode, reused)
        db_path = _db_path or DB_PATH
        db = sqlite3.connect(str(db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")

        while time.time() < deadline:
            # Default check interval (may be overridden based on mode below)
            check_interval = active_interval

            try:
                # ── Read all state in one pass ──
                member = db.execute(
                    "SELECT last_seen, last_read, status_text, status_changed_at, "
                    "messenger_heartbeat, watchdog_heartbeat "
                    "FROM members WHERE channel = ? AND id = ?",
                    (channel, member_id),
                ).fetchone()

                if not member:
                    return {"event": "error", "msg": "Member not found in channel."}

                # Channel status
                ch = db.execute(
                    "SELECT status, ended_by FROM channels WHERE code = ?",
                    (channel,),
                ).fetchone()

                if not ch:
                    return {"event": "channel_gone"}

                if ch["status"] == "ended":
                    ended_by_name = ch["ended_by"]
                    if ch["ended_by"]:
                        ender = db.execute(
                            "SELECT name FROM members WHERE channel = ? AND id = ?",
                            (channel, ch["ended_by"]),
                        ).fetchone()
                        if ender:
                            ended_by_name = ender["name"]
                    return {"event": "channel_ended", "ended_by": ended_by_name}

                # ── Update heartbeat ──
                now_ts = now_iso()
                if own_col:
                    # own_col is validated against VALID_ROLES at function entry
                    sql = f"UPDATE members SET last_seen = ?, {own_col} = ? WHERE channel = ? AND id = ?"
                    db.execute(sql, (now_ts, now_ts, channel, member_id))
                else:
                    db.execute(
                        "UPDATE members SET last_seen = ? WHERE channel = ? AND id = ?",
                        (now_ts, channel, member_id),
                    )
                db.commit()

                # ── Determine mode ──
                sleeping_flag = is_sleeping_flag(member["status_text"])

                # Sleep confirmation: require N seconds of silence after flag set
                if sleeping_flag:
                    status_age = seconds_since(
                        member["status_changed_at"]
                        if "status_changed_at" in member.keys() and member["status_changed_at"]
                        else None
                    )
                    # Latest message from this member
                    latest_own = db.execute(
                        "SELECT created_at FROM messages "
                        "WHERE channel = ? AND member_id = ? ORDER BY id DESC LIMIT 1",
                        (channel, member_id),
                    ).fetchone()
                    last_own_msg_age = seconds_since(
                        latest_own["created_at"] if latest_own else None
                    )

                    sleep_confirmed = (
                        status_age != float("inf")
                        and status_age >= sleep_confirm
                        and last_own_msg_age >= sleep_confirm
                    )
                else:
                    sleep_confirmed = False
                    inconsistency_streak = 0
                    prev_msg_count = None  # reset to prevent stale deltas on re-sleep

                mode = "sleep" if (sleeping_flag and sleep_confirmed) else (
                    "idle" if sleeping_flag else "active"
                )
                check_interval = idle_interval if mode in ("idle", "sleep") else active_interval

                # ── Check 1: New messages from others ──
                if local_hwm is None:
                    local_hwm = member["last_read"]

                unread = db.execute(
                    "SELECT id, member_id, member_name, content, created_at "
                    "FROM messages WHERE channel = ? AND id > ? AND member_id != ? "
                    "ORDER BY id",
                    (channel, local_hwm, member_id),
                ).fetchall()

                if unread:
                    local_hwm = max(m["id"] for m in unread)
                    if should_return("new_messages"):
                        msg_ids = [m["id"] for m in unread]
                        return {
                            "event": "new_messages",
                            "mode": mode,
                            "message_ids": msg_ids,
                            "count": len(msg_ids),
                            "msg": f"{len(msg_ids)} new message(s) detected. Poll MCP for content.",
                        }
                    # Not watching new_messages — continue loop

                # ── Check 2: Flag inconsistency (idle/sleep modes) ──
                # (Check numbering: 1=messages, 2=flag, 3=cadence, 4=peer heartbeat)
                if sleeping_flag:
                    own_msg_count_row = db.execute(
                        "SELECT COUNT(*) as cnt FROM messages "
                        "WHERE channel = ? AND member_id = ?",
                        (channel, member_id),
                    ).fetchone()
                    own_msg_count = own_msg_count_row["cnt"] if own_msg_count_row else 0

                    if prev_msg_count is not None and own_msg_count > prev_msg_count:
                        msgs_sent = own_msg_count - prev_msg_count
                        if msgs_sent > 1:  # >1 not >=1: send() auto-clears sleeping flag, so 1 msg is expected
                            inconsistency_streak += 1
                            if inconsistency_streak >= 2 and should_return("flag_inconsistency"):
                                prev_msg_count = own_msg_count
                                return {
                                    "event": "flag_inconsistency",
                                    "mode": mode,
                                    "msgs_sent": msgs_sent,
                                    "msg": (
                                        f"Status says sleeping but {msgs_sent} messages sent "
                                        f"across 2 consecutive checks. Update status or confirm idle."
                                    ),
                                }
                        else:
                            inconsistency_streak = 0
                    else:
                        inconsistency_streak = 0

                    prev_msg_count = own_msg_count

                # ── Check 3: Cadence silence (active mode only) ──
                if mode == "active":
                    latest_own = db.execute(
                        "SELECT created_at FROM messages "
                        "WHERE channel = ? AND member_id = ? ORDER BY id DESC LIMIT 1",
                        (channel, member_id),
                    ).fetchone()
                    cadence_gap = seconds_since(
                        latest_own["created_at"] if latest_own else None
                    )
                    if cadence_gap > cadence_threshold and should_return("cadence"):
                        return {
                            "event": "cadence",
                            "mode": mode,
                            "gap_seconds": round(cadence_gap),
                            "msg": (
                                f"No status post in {round(cadence_gap)}s. "
                                f"Post an update with confidence level."
                            ),
                        }

                # ── Check 4: Peer sentinel heartbeat (if role set) ──
                if peer_col and (time.time() - start_time) > PEER_DEAD_STARTUP_GRACE:
                    peer_hb = member[peer_col] if peer_col in member.keys() else None
                    peer_gap = seconds_since(peer_hb)
                    if peer_gap > PEER_DEAD_THRESHOLD:
                        peer_dead_streak += 1
                        if peer_dead_streak >= 2 and should_return("peer_dead"):
                            peer_name = PEER_ROLES.get(role, "unknown")
                            return {
                                "event": "peer_dead",
                                "peer": peer_name,
                                "gap_seconds": round(peer_gap),
                                "mode": mode,
                                "msg": (
                                    f"{peer_name} sentinel heartbeat stale "
                                    f"({round(peer_gap)}s). Peer may be dead."
                                ),
                            }
                    else:
                        peer_dead_streak = 0

            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    return {"event": "error", "msg": "Database not initialized."}
                db_error_streak += 1
                if db_error_streak >= 10:
                    return {
                        "event": "error",
                        "msg": f"Persistent DB failure after {db_error_streak} consecutive errors: {e}",
                    }
                # Transient DB error — retry on next cycle
            else:
                db_error_streak = 0

            time.sleep(check_interval)

    finally:
        if db:
            db.close()

    return {
        "event": "cap",
        "runtime_seconds": round(max_runtime),
        "msg": "Sentinel max runtime reached. Relaunch.",
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: roam_hive_mind_sentinel.py <channel> <member_id> [options]"
        }))
        sys.exit(1)

    channel = sys.argv[1]
    member_id = sys.argv[2]

    # Parse optional flags
    opts = {
        "max_runtime": MAX_RUNTIME_S,
        "heartbeat_threshold": DEFAULT_HEARTBEAT_THRESHOLD,
        "idle_heartbeat_threshold": DEFAULT_IDLE_HEARTBEAT_THRESHOLD,
        "cadence_threshold": DEFAULT_CADENCE_THRESHOLD,
        "sleep_confirm": DEFAULT_SLEEP_CONFIRM,
        "active_interval": DEFAULT_ACTIVE_INTERVAL,
        "idle_interval": DEFAULT_IDLE_INTERVAL,
    }

    flag_map = {
        "--max-runtime": ("max_runtime", 60),
        "--heartbeat-threshold": ("heartbeat_threshold", 30),
        "--idle-heartbeat-threshold": ("idle_heartbeat_threshold", 60),
        "--cadence-threshold": ("cadence_threshold", 30),
        "--sleep-confirm": ("sleep_confirm", 10),
        "--active-interval": ("active_interval", 1),
        "--idle-interval": ("idle_interval", 5),
    }

    watch_events = None  # default: return on all events

    args = sys.argv[3:]
    for i, arg in enumerate(args):
        if arg == "--watch" and i + 1 < len(args):
            watch_events = [e.strip() for e in args[i + 1].split(",") if e.strip()]
        elif arg in flag_map and i + 1 < len(args):
            key, minimum = flag_map[arg]
            try:
                opts[key] = max(minimum, int(args[i + 1]))
            except ValueError:
                pass

    result = sentinel(channel, member_id, watch_events=watch_events, **opts)
    print(json.dumps(result))
