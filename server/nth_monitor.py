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

Filter modes (pick ONE; default = fire on everything):

    --filter at            — wake on @pings (mentions including me) only.
                              Silent for broadcasts and for #pound refs.
    --filter at+broadcast  — wake on @pings OR broadcasts (empty mentions).
                              Same shape as the legacy --mention-filter.
    --filter at+pound      — wake on @pings OR #pound refs. Silent on
                              broadcasts.
    --filter at+pound+broadcast
                            — wake on anything addressed to me or broadcast.
    --filter pound         — wake ONLY on #pound refs. For agents that
                              came online to catch up on background chatter.
    --filter all           — wake on everything. (Same as no --filter flag.)

The legacy --mention-filter flag is kept as an alias for
--filter at+broadcast.

All unread messages advance the local watermark regardless of filter
outcome, so nothing is re-surfaced.

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

ACTIVE_INTERVAL = 0.5
IDLE_INTERVAL = 3.0
HEARTBEAT_INTERVAL = 10.0
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


FILTER_MODES = {
    # name → set of categories that WAKE the agent
    "all":                    {"at", "pound", "broadcast", "other"},
    "at":                     {"at"},
    "at+broadcast":           {"at", "broadcast"},
    "at+pound":               {"at", "pound"},
    "at+pound+broadcast":     {"at", "pound", "broadcast"},
    "pound":                  {"pound"},
}


def classify_message(member_id, mentions_raw, refs_raw):
    """Return one of 'at' | 'pound' | 'broadcast' | 'other'.

    'at'        — member_id is in the mentions array (pinged)
    'pound'     — member_id is in refs only (referenced, not pinged)
    'broadcast' — both arrays are empty (talking to the room)
    'other'     — someone else was pinged/referenced and member wasn't
    """
    try:
        mention_list = json.loads(mentions_raw) if mentions_raw else []
    except (ValueError, TypeError):
        mention_list = []
    try:
        ref_list = json.loads(refs_raw) if refs_raw else []
    except (ValueError, TypeError):
        ref_list = []
    if member_id in mention_list:
        return "at"
    if member_id in ref_list:
        return "pound"
    if not mention_list and not ref_list:
        return "broadcast"
    return "other"


def monitor(channel, member_id, filter_mode="all", _db_path=None):
    local_hwm = None
    cadence_fired = False
    last_heartbeat_mono = 0.0
    last_heartbeat_wall = 0.0
    db_error_streak = 0

    db_path = _db_path or DB_PATH
    db = sqlite3.connect(str(db_path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    # synchronous=NORMAL is safe under WAL: we lose at most the most recent
    # commit on a hard crash, and the only thing we commit here is a heartbeat
    # timestamp — recomputed on the next tick. Dropping per-commit fsync is
    # what makes sub-second polling cheap on laptop SSDs.
    db.execute("PRAGMA synchronous=NORMAL")
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
                    ender_name = None
                    if ch["ended_by"]:
                        ender = db.execute(
                            "SELECT name FROM members WHERE channel = ? AND id = ?",
                            (channel, ch["ended_by"]),
                        ).fetchone()
                        # If the ender has been culled/deleted since they called
                        # trio_end, fall back to a readable label instead of leaking
                        # the raw member_id into the event payload.
                        ender_name = ender["name"] if ender else "(culled member)"
                    emit({"event": "channel_ended", "ended_by": ender_name})
                    return

                # Decouple heartbeat writes from poll rate. At 0.5s active polling
                # we'd otherwise do ~172k fsync-bearing commits/day just to bump a
                # timestamp. The server's _sentinel_nag() threshold is 300s, so
                # writing once every HEARTBEAT_INTERVAL (~10s) is 30× margin.
                #
                # Use BOTH monotonic and wall clock. Monotonic wins for tick-to-tick
                # cadence (cheap, immune to wall-clock jumps) but freezes across
                # host suspend — a laptop sleep for 10 min would leave the server
                # seeing our heartbeat as stale while our monotonic delta only
                # counts the ticks we actually ran. The wall-clock fallback forces
                # a fresh write whenever real time has elapsed past the threshold.
                mono = time.monotonic()
                wall = time.time()
                if (mono - last_heartbeat_mono >= HEARTBEAT_INTERVAL
                        or wall - last_heartbeat_wall >= HEARTBEAT_INTERVAL):
                    now_ts = now_iso()
                    db.execute(
                        "UPDATE members SET last_seen = ?, "
                        "messenger_heartbeat = ?, watchdog_heartbeat = ? "
                        "WHERE channel = ? AND id = ?",
                        (now_ts, now_ts, now_ts, channel, member_id),
                    )
                    db.commit()
                    last_heartbeat_mono = mono
                    last_heartbeat_wall = wall

                sleeping = is_sleeping(member["status_text"])
                check_interval = IDLE_INTERVAL if sleeping else ACTIVE_INTERVAL

                # --- New messages ---
                # Reconcile local_hwm against the live DB watermark on every
                # tick, not just at init. The agent can advance its own watermark
                # via trio_ack (server writes members.last_read + sessions.last_read)
                # while we're asleep between polls; without this reconciliation
                # we'd re-notify on messages the agent already acked. We take the
                # max so we never regress.
                legacy_hwm = member["last_read"] or 0
                try:
                    sess_row = db.execute(
                        "SELECT MAX(last_read) AS hwm FROM sessions "
                        "WHERE channel = ? AND member_id = ? "
                        "AND revoked_at IS NULL",
                        (channel, member_id),
                    ).fetchone()
                    sess_hwm = (sess_row["hwm"] or 0) if sess_row else 0
                except sqlite3.OperationalError:
                    sess_hwm = 0
                external_hwm = max(legacy_hwm, sess_hwm)
                local_hwm = external_hwm if local_hwm is None else max(local_hwm, external_hwm)

                # Pull refs alongside mentions so the filter can distinguish
                # @pings from #pound refs. If the refs column is missing (older
                # schema), treat it as empty and fall back cleanly.
                try:
                    unread = db.execute(
                        "SELECT id, mentions, refs, member_name, content FROM messages "
                        "WHERE channel = ? AND id > ? AND member_id != ? "
                        "ORDER BY id",
                        (channel, local_hwm, member_id),
                    ).fetchall()
                except sqlite3.OperationalError:
                    unread = db.execute(
                        "SELECT id, mentions, member_name, content FROM messages "
                        "WHERE channel = ? AND id > ? AND member_id != ? "
                        "ORDER BY id",
                        (channel, local_hwm, member_id),
                    ).fetchall()

                if unread:
                    local_hwm = max(m["id"] for m in unread)

                    wake_categories = FILTER_MODES.get(filter_mode, FILTER_MODES["all"])
                    relevant = []
                    for m in unread:
                        mraw = m["mentions"] if "mentions" in m.keys() else ""
                        rraw = m["refs"] if "refs" in m.keys() else ""
                        cat = classify_message(member_id, mraw, rraw)
                        if cat in wake_categories:
                            relevant.append((m, cat))

                    if relevant:
                        # Classify aggregate flags so the agent can skip the
                        # trio_poll round-trip on low-signal wake-ups.
                        has_mentions = any(cat == "at" for _m, cat in relevant)
                        has_refs     = any(cat == "pound" for _m, cat in relevant)
                        from_names = []
                        seen = set()
                        for m, _cat in relevant:
                            n = m["member_name"] or ""
                            if n and n not in seen:
                                seen.add(n)
                                from_names.append(n)
                        latest_content = relevant[-1][0]["content"] or ""
                        preview = latest_content[:80] + ("…" if len(latest_content) > 80 else "")

                        emit({
                            "event": "new_messages",
                            "mode": "idle" if sleeping else "active",
                            "message_ids": [m["id"] for m, _c in relevant],
                            "count": len(relevant),
                            "has_mentions": has_mentions,
                            "has_refs": has_refs,
                            "from_names": from_names,
                            "preview": preview,
                            "filter": filter_mode,
                        })

                # --- Cadence (active mode, no claimed tasks → skip) ---
                # Fire once per silence period if member is in active mode AND holds
                # at least one claimed task. Workers standing by with no task claim
                # don't need a nudge — the idle-reply/auto-clear cycle was producing
                # pure-ceremony cadence pings.
                if not sleeping:
                    try:
                        claimed_count_row = db.execute(
                            "SELECT COUNT(*) AS n FROM tasks "
                            "WHERE channel = ? AND claimed_by = ? AND status = 'claimed'",
                            (channel, member_id),
                        ).fetchone()
                        claimed_count = claimed_count_row["n"] if claimed_count_row else 0
                    except sqlite3.OperationalError:
                        claimed_count = 0

                    if claimed_count > 0:
                        latest_own = db.execute(
                            "SELECT created_at FROM messages "
                            "WHERE channel = ? AND member_id = ? ORDER BY id DESC LIMIT 1",
                            (channel, member_id),
                        ).fetchone()
                        gap = seconds_since(
                            latest_own["created_at"] if latest_own else None
                        )
                        if gap > CADENCE_THRESHOLD and not cadence_fired:
                            emit({"event": "cadence", "gap_seconds": round(gap), "claimed_tasks": claimed_count})
                            cadence_fired = True
                        elif gap < CADENCE_THRESHOLD:
                            cadence_fired = False
                    else:
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


def parse_filter_arg(argv_tail):
    """Return a filter_mode string. Flags accepted:
      --filter MODE        (where MODE is a key of FILTER_MODES)
      --mention-filter     (legacy alias for --filter at+broadcast)
    """
    i = 0
    while i < len(argv_tail):
        arg = argv_tail[i]
        if arg == "--filter":
            if i + 1 >= len(argv_tail):
                raise ValueError("--filter requires a value")
            mode = argv_tail[i + 1]
            if mode not in FILTER_MODES:
                raise ValueError(
                    f"unknown filter mode '{mode}'. "
                    f"valid: {', '.join(sorted(FILTER_MODES.keys()))}"
                )
            return mode
        if arg == "--mention-filter":
            return "at+broadcast"
        i += 1
    return "all"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        emit({"event": "error",
              "msg": "Usage: nth_monitor.py <channel> <member_id> "
                     "[--filter MODE | --mention-filter]"})
        sys.exit(1)

    channel_arg = sys.argv[1]
    member_arg = sys.argv[2]
    try:
        filter_arg = parse_filter_arg(sys.argv[3:])
    except ValueError as e:
        emit({"event": "error", "msg": str(e)})
        sys.exit(1)

    try:
        monitor(channel_arg, member_arg, filter_mode=filter_arg)
    except KeyboardInterrupt:
        pass
