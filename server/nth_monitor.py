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

Filter modes (pick ONE; default = all):

    --filter all    — wake on every peer message. (Default, no flag needed.)
    --filter about  — wake on any message ABOUT me: @pings or #pounds.
                       No wake on unrelated chatter between other members.
    --filter at     — wake only on @pings. #pound refs are silent.

Bangs (`!name` / `!all`) ALWAYS wake the target regardless of filter. They
are the last-resort / channel-close signal and deliberately bypass every
opt-out. Agents cannot suppress bangs; using bang for routine messages is
abusive to the room.

The legacy --mention-filter flag is kept as an alias for --filter about.

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


FILTER_MODES = ("all", "about", "at")


def _parse_id_list(raw):
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def should_wake(member_id, mentions_raw, refs_raw, bangs_raw, filter_mode):
    """Decide whether a single message should wake this member under the
    chosen filter. Bangs ALWAYS wake — they bypass every filter by design."""
    bang_list = _parse_id_list(bangs_raw)
    if member_id in bang_list:
        return True, "bang"
    mention_list = _parse_id_list(mentions_raw)
    ref_list = _parse_id_list(refs_raw)
    if filter_mode == "all":
        return True, "at" if member_id in mention_list else ("pound" if member_id in ref_list else "ambient")
    if filter_mode == "about":
        if member_id in mention_list:
            return True, "at"
        if member_id in ref_list:
            return True, "pound"
        return False, None
    if filter_mode == "at":
        if member_id in mention_list:
            return True, "at"
        return False, None
    # Unknown mode — fail open (wake on everything) rather than silencing.
    return True, "ambient"


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
            # Default poll cadence. Reassigned below once we know whether the
            # member is sleeping, but needs a value here so the trailing
            # time.sleep(check_interval) is safe even when the try-block bails
            # on OperationalError before reaching the sleeping-check.
            check_interval = ACTIVE_INTERVAL
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
                    # Best-effort filter_mode write (added v7.2). Older DBs
                    # without the column drop back to the pre-v7.2 heartbeat.
                    try:
                        db.execute(
                            "UPDATE members SET last_seen = ?, "
                            "messenger_heartbeat = ?, watchdog_heartbeat = ?, "
                            "filter_mode = ? "
                            "WHERE channel = ? AND id = ?",
                            (now_ts, now_ts, now_ts, filter_mode, channel, member_id),
                        )
                    except sqlite3.OperationalError:
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

                # Pull mentions + refs + bangs alongside the message. If the
                # schema is pre-v7.1 (no refs) or pre-v7.2 (no bangs), the
                # OperationalError drops us to progressively older SELECTs.
                # refs/bangs are both treated as empty when missing — agents
                # on old DBs just get the pre-bang behavior.
                try:
                    unread = db.execute(
                        "SELECT id, mentions, refs, bangs, member_name, content FROM messages "
                        "WHERE channel = ? AND id > ? AND member_id != ? "
                        "ORDER BY id",
                        (channel, local_hwm, member_id),
                    ).fetchall()
                except sqlite3.OperationalError:
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

                    mode = filter_mode if filter_mode in FILTER_MODES else "all"
                    relevant = []
                    for m in unread:
                        mraw = m["mentions"] if "mentions" in m.keys() else ""
                        rraw = m["refs"] if "refs" in m.keys() else ""
                        braw = m["bangs"] if "bangs" in m.keys() else ""
                        wake, kind = should_wake(member_id, mraw, rraw, braw, mode)
                        if wake:
                            relevant.append((m, kind))

                    if relevant:
                        # Aggregate flags so the agent can skip trio_poll on
                        # low-signal wake-ups.
                        has_bangs    = any(k == "bang"  for _m, k in relevant)
                        has_mentions = any(k == "at"    for _m, k in relevant)
                        has_refs     = any(k == "pound" for _m, k in relevant)
                        from_names = []
                        seen = set()
                        for m, _kind in relevant:
                            n = m["member_name"] or ""
                            if n and n not in seen:
                                seen.add(n)
                                from_names.append(n)
                        latest_content = relevant[-1][0]["content"] or ""
                        preview = latest_content[:80] + ("…" if len(latest_content) > 80 else "")

                        emit({
                            "event": "new_messages",
                            "mode": "idle" if sleeping else "active",
                            "message_ids": [m["id"] for m, _k in relevant],
                            "count": len(relevant),
                            "has_bangs": has_bangs,
                            "has_mentions": has_mentions,
                            "has_refs": has_refs,
                            "from_names": from_names,
                            "preview": preview,
                            "filter": mode,
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
                # Release any implicit BEGIN started by the heartbeat UPDATE
                # before the exception. Without this, a failed commit leaves
                # the connection holding the WAL writer lock across the sleep
                # until close() — which is exactly the starvation we're trying
                # to avoid in peers. Best-effort: a rollback that itself fails
                # just drops us to the next loop tick.
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
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
      --filter MODE        where MODE is one of FILTER_MODES (all, about, at).
      --mention-filter     legacy alias for --filter about.
      --filter at+broadcast / at+pound / at+pound+broadcast / pound
                           pre-v7.2 names; mapped to the nearest current mode.
    """
    legacy_map = {
        "at+broadcast":        "about",
        "at+pound":            "about",
        "at+pound+broadcast":  "about",
        "pound":               "about",
    }
    i = 0
    while i < len(argv_tail):
        arg = argv_tail[i]
        if arg == "--filter":
            if i + 1 >= len(argv_tail):
                raise ValueError("--filter requires a value")
            mode = argv_tail[i + 1]
            if mode in FILTER_MODES:
                return mode
            if mode in legacy_map:
                return legacy_map[mode]
            raise ValueError(
                f"unknown filter mode '{mode}'. "
                f"valid: {', '.join(FILTER_MODES)}"
            )
        if arg == "--mention-filter":
            return "about"
        i += 1
    return "all"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        emit({"event": "error",
              "msg": "Usage: nth_monitor.py <channel> <member_id> "
                     "[--filter all|about|at | --mention-filter]"})
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
