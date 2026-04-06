"""
Smart watchdog for Trio agents — mode-aware monitoring of heartbeat and cadence.

Runs inside a background Agent. Checks SQLite every 30 seconds.
Only returns (nags the parent) when something has actually gone wrong.

MODE AWARENESS: Reads the member's status_text to determine parent mode.
  - Working mode (default): checks both heartbeat (2min) and cadence (3min)
  - Sleeping mode (status contains "idle" or "standing by"): checks heartbeat
    only with a wider threshold (5min — agent-monitor died). Cadence is
    expected to be silent during idle, so no nag.

While everything is healthy, this costs zero LLM tokens — just a
sleeping process doing periodic SQLite reads.

Usage:
    python roam_hive_mind_watchdog.py <channel> <member_id> [options]

Options:
    --heartbeat-threshold SECONDS  (default: 120 — working mode)
    --idle-heartbeat-threshold SECONDS  (default: 300 — sleeping mode)
    --cadence-threshold SECONDS    (default: 180 — working mode only)
    --check-interval SECONDS       (default: 30)
    --max-checks N                 (default: 600 — ~5 hours at 30s intervals)

Output (JSON, one line):
    {"nag": "heartbeat", "mode": "working", "gap_seconds": 145, "msg": "..."}
    {"nag": "heartbeat", "mode": "sleeping", "gap_seconds": 310, "msg": "..."}
    {"nag": "cadence", "mode": "working", "gap_seconds": 200, "msg": "..."}
    {"nag": "cap", "checks": 600, "msg": "Watchdog cycle cap reached. Relaunch."}
"""

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "roam" / "roam.db"

DEFAULT_HEARTBEAT_THRESHOLD = 120       # 2 minutes (working mode)
DEFAULT_IDLE_HEARTBEAT_THRESHOLD = 300  # 5 minutes (sleeping mode — agent-monitor died)
DEFAULT_CADENCE_THRESHOLD = 180         # 3 minutes (working mode only)
DEFAULT_CHECK_INTERVAL = 30             # seconds between checks
DEFAULT_MAX_CHECKS = 600                # ~5 hours

SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def seconds_since(iso_timestamp):
    """Seconds elapsed since an ISO timestamp."""
    if not iso_timestamp:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def is_sleeping(status_text):
    """Determine if the parent is in sleeping/idle mode from status_text."""
    if not status_text:
        return False
    lower = status_text.lower()
    return any(kw in lower for kw in SLEEPING_KEYWORDS)


DEFAULT_SLEEP_CONFIRM_SECONDS = 60  # silence required before sleep thresholds apply


def watch(channel, member_id, heartbeat_threshold, idle_heartbeat_threshold,
          cadence_threshold, check_interval, max_checks,
          sleep_confirm_seconds=DEFAULT_SLEEP_CONFIRM_SECONDS):
    checks = 0
    # Track state across iterations for flag inconsistency + sleep confirmation
    prev_msg_count = None       # message count at last check
    sleep_flag_set_at = None    # wall-clock time when sleeping flag first seen (fallback)
    sleep_confirmed = False     # True after silence holds for confirm period
    inconsistency_count = 0     # consecutive checks with flag inconsistency

    while checks < max_checks:
        checks += 1
        db = get_db()
        try:
            # Read member state in one pass
            member = db.execute(
                "SELECT last_seen, status_text, status_changed_at FROM members WHERE channel = ? AND id = ?",
                (channel, member_id),
            ).fetchone()

            if not member:
                return {"nag": "error", "msg": "Member not found in channel."}

            sleeping_flag = is_sleeping(member["status_text"])

            # Count this member's messages (for flag inconsistency detection)
            msg_count_row = db.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE channel = ? AND member_id = ?",
                (channel, member_id),
            ).fetchone()
            msg_count = msg_count_row["cnt"] if msg_count_row else 0

            # Latest message timestamp (for cadence + flag checks)
            latest_msg = db.execute(
                "SELECT created_at FROM messages "
                "WHERE channel = ? AND member_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (channel, member_id),
            ).fetchone()
            last_msg_age = seconds_since(
                latest_msg["created_at"] if latest_msg else None
            )

            # ── Sleep confirmation logic ──
            # When sleeping flag appears, require N seconds of silence before
            # relaxing thresholds. Use status_changed_at from DB as the
            # authoritative transition timestamp (survives watchdog restarts).
            now = time.time()
            if sleeping_flag:
                # Determine when the sleeping flag was set
                status_age = seconds_since(
                    member["status_changed_at"]
                    if "status_changed_at" in member.keys() and member["status_changed_at"]
                    else None
                )
                # Fallback to local tracking if DB has no timestamp
                if status_age == float("inf"):
                    if sleep_flag_set_at is None:
                        sleep_flag_set_at = now
                    status_age = now - sleep_flag_set_at

                # Check for flag inconsistency: flagged sleeping but actively sending.
                # Require 2+ messages in one check interval (1 message = normal
                # wake-respond-sleep cycle). Also require sustained inconsistency —
                # only nag if this is the second consecutive observation.
                if prev_msg_count is not None and msg_count > prev_msg_count:
                    msgs_sent = msg_count - prev_msg_count
                    if msgs_sent > 1:
                        inconsistency_count += 1
                        if inconsistency_count >= 2:
                            # Sustained inconsistency — nag
                            prev_msg_count = msg_count
                            return {
                                "nag": "flag_inconsistency",
                                "mode": "sleeping",
                                "msgs_sent_while_sleeping": msgs_sent,
                                "msg": (f"You're flagged as sleeping but sent {msgs_sent} message(s) "
                                        f"across 2 consecutive checks. Update your status to working, "
                                        f"or confirm you're going back to sleep."),
                            }
                    else:
                        inconsistency_count = 0
                else:
                    inconsistency_count = 0

                # Confirm sleep: status has been sleeping for N seconds AND
                # no messages sent in that window
                sleep_confirmed = (
                    status_age >= sleep_confirm_seconds
                    and last_msg_age >= sleep_confirm_seconds
                )
            else:
                # Not sleeping — reset tracking
                sleep_flag_set_at = None
                sleep_confirmed = False
                inconsistency_count = 0

            # Effective mode: only treat as sleeping if confirmed
            sleeping = sleeping_flag and sleep_confirmed
            mode = "sleeping" if sleeping else "working"

            # ── Check 1: heartbeat ──
            heartbeat_gap = seconds_since(member["last_seen"])
            effective_threshold = idle_heartbeat_threshold if sleeping else heartbeat_threshold

            if heartbeat_gap > effective_threshold:
                if sleeping:
                    msg = (f"Agent-monitor may have died. last_seen is {round(heartbeat_gap)}s ago "
                           f"(threshold: {effective_threshold}s). Restart monitor or resume polling.")
                else:
                    msg = f"You stopped polling. last_seen is {round(heartbeat_gap)}s ago. Resume now."
                return {
                    "nag": "heartbeat",
                    "mode": mode,
                    "gap_seconds": round(heartbeat_gap),
                    "msg": msg,
                }

            # ── Check 2: cadence — only in working mode ──
            if not sleeping:
                if last_msg_age > cadence_threshold:
                    return {
                        "nag": "cadence",
                        "mode": mode,
                        "gap_seconds": round(last_msg_age),
                        "msg": f"You haven't posted in {round(last_msg_age)}s. Post a status update with confidence level.",
                    }

            # ── Check 3: channel still active ──
            ch = db.execute(
                "SELECT status FROM channels WHERE code = ?",
                (channel,),
            ).fetchone()
            if not ch or ch["status"] == "ended":
                return {"nag": "channel_ended", "msg": "Channel has ended. Watchdog stopping."}

            prev_msg_count = msg_count

        finally:
            db.close()

        time.sleep(check_interval)

    return {
        "nag": "cap",
        "checks": checks,
        "msg": "Watchdog cycle cap reached. Relaunch.",
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: roam_hive_mind_watchdog.py <channel> <member_id> [options]"
        }))
        sys.exit(1)

    channel = sys.argv[1]
    member_id = sys.argv[2]

    # Parse optional flags
    heartbeat_threshold = DEFAULT_HEARTBEAT_THRESHOLD
    idle_heartbeat_threshold = DEFAULT_IDLE_HEARTBEAT_THRESHOLD
    cadence_threshold = DEFAULT_CADENCE_THRESHOLD
    check_interval = DEFAULT_CHECK_INTERVAL
    max_checks = DEFAULT_MAX_CHECKS

    args = sys.argv[3:]
    for i, arg in enumerate(args):
        try:
            if arg == "--heartbeat-threshold" and i + 1 < len(args):
                heartbeat_threshold = max(30, int(args[i + 1]))
            elif arg == "--idle-heartbeat-threshold" and i + 1 < len(args):
                idle_heartbeat_threshold = max(60, int(args[i + 1]))
            elif arg == "--cadence-threshold" and i + 1 < len(args):
                cadence_threshold = max(30, int(args[i + 1]))
            elif arg == "--check-interval" and i + 1 < len(args):
                check_interval = max(5, int(args[i + 1]))
            elif arg == "--max-checks" and i + 1 < len(args):
                max_checks = max(1, int(args[i + 1]))
        except ValueError:
            pass

    result = watch(channel, member_id, heartbeat_threshold,
                   idle_heartbeat_threshold, cadence_threshold,
                   check_interval, max_checks)
    print(json.dumps(result))
