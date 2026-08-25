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
    {"event": "culled", "member_id": "...", "channel": "..."}
    {"event": "error", "msg": "..."}

The `culled` event is TERMINAL, like `channel_ended`/`channel_gone`: the
member row disappeared AFTER we'd seen it present (an operator cull hard-
DELETEs the row), so the script exits. A missing row we've never yet seen
is treated as the transient join race instead (`error` + retry).

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
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nth_constants import (SLEEPING_KEYWORDS, NTH_VERSION, project_context,
                           can_see)

# Own-session statusline snapshot (see nth_spoke_monitor.read_own_context).
_CTX_DIR = Path(os.environ.get("XDG_STATE_HOME",
                               str(Path.home() / ".local" / "state"))) / "claude-context"
_OWN_SESSION_ID = (os.environ.get("CLAUDE_CODE_SESSION_ID")
                   or os.environ.get("CLAUDE_SESSION_ID", ""))


def read_own_context():
    if not _OWN_SESSION_ID:
        return None
    path = _CTX_DIR / (_OWN_SESSION_ID + ".json")
    try:
        if time.time() - path.stat().st_mtime > 120:
            return None
        raw = path.read_text(encoding="utf-8")
        json.loads(raw)
        return raw
    except (OSError, ValueError):
        return None

DB_PATH = Path.home() / ".claude" / "nth" / "nth.db"

ACTIVE_INTERVAL = 0.5
IDLE_INTERVAL = 3.0
HEARTBEAT_INTERVAL = 10.0
CADENCE_THRESHOLD = 600
# Tap the parent session before the Anthropic prompt-cache TTL (1h) expires.
# 55 min gives a 5-min buffer for clock skew, network latency, and the time
# the agent takes to handle the event. Fires once per quiet period.
KEEPALIVE_THRESHOLD = 55 * 60
# Give up on tapping when the channel has been genuinely dead for this long
# — no peer messages (regardless of whether they mention us). At 1M-tier
# pricing a typical tap costs ~$1.25/hr; a full rewrite on return is ~$24.
# Break-even lands around 17h, but the pathological-idle losses scale with
# absolute idle time. 7h is well inside the break-even margin and caps the
# worst-case "channel abandoned overnight" waste at a few taps worth of
# spend. On eventual re-engagement the agent pays one rewrite, which the
# taps we would have spent more than covered already.
KEEPALIVE_GIVEUP = 7 * 3600


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


def gap_for_emit(gap):
    """JSON-safe rounding for a gap-seconds diagnostic field.

    seconds_since() returns float("inf") as the "never happened" sentinel
    (e.g. a member who was never sigil-engaged). inf compares correctly in
    min()/> but round(inf) raises OverflowError, so guard the emit side:
    inf -> None ("never"), otherwise the rounded integer seconds.
    """
    return None if gap == float("inf") else round(gap)


def build_keepalive_event(own_gap, engaged_gap):
    """Construct the keepalive event dict emitted by monitor().

    Kept as a module-level helper (rather than inline in the loop) so the
    regression test can exercise the exact production construction — both
    gap fields are routed through gap_for_emit(), so an inf engaged_gap
    serializes as null instead of raising OverflowError on round(inf).
    """
    return {
        "event": "keepalive",
        "gap_seconds": gap_for_emit(own_gap),
        "threshold_seconds": KEEPALIVE_THRESHOLD,
        "engaged_gap_seconds": gap_for_emit(engaged_gap),
    }


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


# Sentinel for "this tick could not read the sessions table" — distinct from
# None, which legitimately means "no such row" and IS a revocation signal.
_SESSION_CHECK_SKIP = object()


def _sessions_table_present(db):
    """Settle ONCE whether this database has a sessions table.

    Deciding it per-tick from an OperationalError conflates a missing table
    with `database is locked`, which is routine on a busy hub — and the old
    code turned one transient lock into a permanently disabled check.
    """
    try:
        cols = db.execute("PRAGMA table_info(sessions)").fetchall()
    except sqlite3.OperationalError:
        return False
    return any((c["name"] if hasattr(c, "keys") else c[1]) == "revoked_at"
               for c in cols)


def _revocation_event(db, member_id, channel):
    """Classify a revoked/absent token, then describe it honestly.

    Revocation has several causes and they call for opposite actions, so the
    message has to name the right one:

      * archived — every session revoked, members deactivated. Reconnecting
        would partially UNDO the archive: nth_connect has no archived_at gate,
        so a reclaim re-inserts the inbox membership and flips active back to 1.
        This case must be checked FIRST and must say "do not reconnect".
      * a newer live primary session holds this identity — either a genuine twin
        or this same process reconnecting. Those are indistinguishable at the DB
        level (a same-session reconnect produces exactly the displaced state,
        and fingerprint cannot discriminate them — two processes resuming one
        Claude session id share it), but the required ACTION is the same: this
        monitor must die. Only the wording has to admit both possibilities.
      * neither — an idle reap, or a revoke with no successor. Reconnecting is
        the correct recovery, so say so.
    """
    base = {"event": "session_revoked", "member_id": member_id,
            "channel": channel}
    try:
        archived = db.execute(
            "SELECT 1 FROM agents WHERE id = ? AND archived_at IS NOT NULL",
            (member_id,)).fetchone()
    except sqlite3.OperationalError:
        archived = None            # pre-archive schema: cannot be archived.
    if archived:
        return {**base, "reason": "archived",
                "msg": ("This agent was archived: every session was revoked "
                        "and its placements deactivated. Stop working and do "
                        "NOT reconnect — reconnecting would partially undo the "
                        "archive.")}
    try:
        successor = db.execute(
            "SELECT 1 FROM sessions WHERE member_id = ? AND channel = ? "
            "AND role = 'primary' AND revoked_at IS NULL LIMIT 1",
            (member_id, channel)).fetchone()
    except sqlite3.OperationalError:
        successor = None
    if successor:
        return {**base, "reason": "displaced",
                "msg": ("This token is no longer valid — a newer session holds "
                        "this identity in this channel. If that is you (you "
                        "just reconnected), relaunch the monitor with the new "
                        "token. If not, another process is this member now: "
                        "stop working and do not reconnect.")}
    return {**base, "reason": "invalidated",
            "msg": ("This session token is no longer valid and nothing has "
                    "replaced it — most likely an idle reap. If you are still "
                    "this member, reconnect and relaunch the monitor.")}


def monitor(channel, member_id, filter_mode="all", _db_path=None,
            session_token=""):
    local_hwm = None
    member_missing_streak = 0
    member_seen = False
    cadence_fired = False
    keepalive_fired = False
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

    # Probe the schema ONCE rather than deciding per tick from an exception.
    # Catching a bare OperationalError around the SELECT would also catch
    # `database is locked`, and the fallback path silently uses a different
    # (narrower) mode — so one transient lock would drop messages, and the
    # watermark advances past them regardless, so they never come back.
    try:
        have_requested_col = any(
            r["name"] == "filter_mode_requested"
            for r in db.execute("PRAGMA table_info(members)").fetchall())
    except sqlite3.Error:
        have_requested_col = False

    # Same reasoning for the displacement check: settle the table's existence
    # once, here, so a lock inside the loop is a skipped tick rather than a
    # permanently disabled safety check. No token means no opinion — see the
    # check itself for why guessing which session is ours is unsafe.
    have_sessions = bool(session_token) and _sessions_table_present(db)

    # The mode actually in force. Starts at the launch argument — a value
    # chosen deliberately by whoever launched this agent, which is strictly
    # better information than a hardcoded default — and is replaced by the
    # operator's request whenever there is one.
    effective_mode = filter_mode if filter_mode in FILTER_MODES else "all"
    last_effective_mode = effective_mode
    last_bad_request = None

    try:
        while True:
            # Default poll cadence. Reassigned below once we know whether the
            # member is sleeping, but needs a value here so the trailing
            # time.sleep(check_interval) is safe even when the try-block bails
            # on OperationalError before reaching the sleeping-check.
            check_interval = ACTIVE_INTERVAL
            try:
                member = db.execute(
                    ("SELECT last_seen, last_read, status_text, "
                     "filter_mode_requested "
                     "FROM members WHERE channel = ? AND id = ?")
                    if have_requested_col else
                    ("SELECT last_seen, last_read, status_text "
                     "FROM members WHERE channel = ? AND id = ?"),
                    (channel, member_id),
                ).fetchone()

                if not member:
                    # A missing member row has three possible causes, and
                    # they need opposite handling:
                    #   * cull — the operator DELETEd our row. We were present
                    #     before, so this is permanent: we have been removed.
                    #   * join race — launched before nth_connect committed our
                    #     row. Transient; retry.
                    #   * wrong DB — a quartet spoke pointed the hub-style
                    #     monitor at its own (often stub) database, so the row
                    #     will never appear.
                    # Having seen ourselves present distinguishes the first from
                    # the other two; a repeat count distinguishes the last two.
                    if member_seen:
                        emit({"event": "culled",
                              "member_id": member_id,
                              "channel": channel})
                        return
                    member_missing_streak += 1
                    if member_missing_streak >= 3:
                        emit({
                            "event": "error",
                            "msg": (
                                "Member not found after 3 checks — this DB "
                                "likely never had the channel (quartet "
                                "spokes: use nth_spoke_monitor.py --url "
                                "<hub sse url>). Exiting."
                            ),
                        })
                        sys.exit(1)
                    emit({"event": "error", "msg": "Member not found in channel."})
                    time.sleep(10)
                    continue
                member_missing_streak = 0

                # Displacement. A reclaim revokes the incumbent's primary
                # session (nth_server._mint_session_token's caller), which stops
                # the displaced process from SPEAKING — but nothing here reads a
                # session, so without this check the loser keeps waking on every
                # mention forever, burning a billed turn per wake to discover it
                # cannot answer. It cannot recover either: reconnecting needs a
                # reclaim_secret the winner already rotated away.
                #
                # Only when launched WITH a token. Absent one we cannot tell
                # which session is ours, and guessing (e.g. "a session newer
                # than my start time exists") would kill a healthy agent that
                # merely reconnected mid-run. No token, no opinion — the
                # pre-existing behaviour, which is why old launch commands and
                # legacy member_id-only clients are unaffected.
                #
                # A token absent from the table counts as revoked: rows are
                # deleted only by _reap_sessions, a week after revocation, so an
                # id that is not there cannot become valid again.
                #
                # This check stays BELOW the members lookup above on purpose: a
                # culled or kicked member has no members row, and `culled` is
                # the more specific answer. Falling through to here would
                # mis-report a removal as a displacement.
                #
                # A transient failure must NOT be mistaken for a missing table.
                # `database is locked` raises OperationalError too, and treating
                # that as "old schema, no opinion" silently disabled this check
                # for the life of the process on one busy moment. The table's
                # existence is settled ONCE, before the loop; in here an
                # OperationalError just skips the tick.
                if have_sessions:
                    try:
                        sess = db.execute(
                            "SELECT revoked_at FROM sessions "
                            "WHERE session_token = ? AND channel = ?",
                            (session_token, channel),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        sess = _SESSION_CHECK_SKIP   # locked; try again in a tick
                    if sess is not _SESSION_CHECK_SKIP and (
                            sess is None or sess["revoked_at"] is not None):
                        # Revoked is not a synonym for displaced. _reap_sessions
                        # revokes anything idle a week, archive revokes every
                        # session the agent has, and an agent's own mid-run
                        # reconnect revokes its previous token. Telling all of
                        # those "another process is you now, do not reconnect"
                        # is wrong, and for two of them it is harmful advice.
                        # So establish WHICH it is before speaking.
                        emit(_revocation_event(db, member_id, channel))
                        return

                # Spec beats status. filter_mode_requested is the operator's
                # override; NULL/blank means "no override, keep the launch
                # arg". A value we do not recognise is IGNORED rather than
                # obeyed and rather than reset to the most expensive mode:
                # every spurious wake is a billed turn, so failing to `all`
                # would make a typo cost money. Bangs are unfilterable, so the
                # catastrophic "agent never hears anything" case is already
                # covered without needing to fail wide here.
                if have_requested_col:
                    requested = (member["filter_mode_requested"] or "").strip().lower()
                    if requested in FILTER_MODES:
                        effective_mode = requested
                    elif not requested:
                        effective_mode = (filter_mode if filter_mode in FILTER_MODES
                                          else "all")
                    elif requested != last_bad_request:
                        # Once per distinct bad value, not once per tick: this
                        # loop runs twice a second and every emit is a
                        # notification in the parent session.
                        last_bad_request = requested
                        emit({"event": "error",
                              "msg": (f"unrecognised filter_mode_requested "
                                      f"{requested!r}; keeping {effective_mode}")})
                # Make an operator's change visible in the notification stream
                # instead of leaving it to be inferred from behaviour.
                if effective_mode != last_effective_mode:
                    emit({"event": "filter_mode",
                          "filter": effective_mode,
                          "was": last_effective_mode})
                    last_effective_mode = effective_mode

                # We've observed our own row at least once. Any later
                # disappearance is a cull, not the startup join race.
                member_seen = True

                ch = db.execute(
                    "SELECT status, ended_by FROM channels WHERE code = ?",
                    (channel,),
                ).fetchone()

                if not ch:
                    # Name the likely real cause: this monitor read a DB that
                    # never had the channel. That happens when a quartet SPOKE
                    # launches the hub-style monitor against its local (often
                    # stub) DB — the channel lives in the hub's DB, reachable
                    # only over SSE.
                    emit({
                        "event": "channel_gone",
                        "hint": (
                            "channel not present in this DB. If this session "
                            "reaches the channel via an SSE MCP server "
                            "(nth-qweb), you are a spoke: use "
                            "nth_spoke_monitor.py --url <hub sse url> instead."
                        ),
                    })
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
                    # PUBLISH the effective mode. filter_mode is status, not
                    # control: peers read it off the roster to decide whether
                    # an ambient post will actually be heard before spending
                    # the tokens to write it, so it has to say what this
                    # monitor is really doing — which is effective_mode, not
                    # the launch argument it may have been overridden by.
                    try:
                        db.execute(
                            "UPDATE members SET last_seen = ?, "
                            "messenger_heartbeat = ?, watchdog_heartbeat = ?, "
                            "filter_mode = ? "
                            "WHERE channel = ? AND id = ?",
                            (now_ts, now_ts, now_ts, effective_mode, channel, member_id),
                        )
                    except sqlite3.OperationalError:
                        db.execute(
                            "UPDATE members SET last_seen = ?, "
                            "messenger_heartbeat = ?, watchdog_heartbeat = ? "
                            "WHERE channel = ? AND id = ?",
                            (now_ts, now_ts, now_ts, channel, member_id),
                        )
                    # Statusline relay for this member (best-effort; the
                    # column exists on v7.3.1+ DBs).
                    own_ctx = read_own_context()
                    # Same projection + size cap the relayed path applies in
                    # nth_server.nth_poll — one column, one policy. A
                    # non-dict payload used to raise TypeError straight out
                    # of the loop and kill the monitor.
                    if own_ctx and len(own_ctx) < 16384:
                        try:
                            import json as _json
                            ctx = project_context(_json.loads(own_ctx))
                            if ctx is not None:
                                ctx["_relayed_at"] = now_ts
                                db.execute(
                                    "UPDATE members SET context_json = ? "
                                    "WHERE channel = ? AND id = ?",
                                    (_json.dumps(ctx), channel, member_id),
                                )
                        except (ValueError, TypeError, sqlite3.OperationalError):
                            pass
                    # v7.3 fleet check-in: one row per (host, "monitor").
                    # Best-effort — the nodes table only exists once a v7.3+
                    # server has touched this DB, and fleet bookkeeping must
                    # never break the notification loop.
                    try:
                        pyv = ".".join(str(p) for p in sys.version_info[:3])
                        db.execute(
                            "INSERT INTO nodes (hostname, transport, nth_version, "
                            "python, pid, last_seen) VALUES (?, 'monitor', ?, ?, ?, ?) "
                            "ON CONFLICT(hostname, transport) DO UPDATE SET "
                            "nth_version = excluded.nth_version, "
                            "python = excluded.python, pid = excluded.pid, "
                            "last_seen = excluded.last_seen",
                            (socket.gethostname(), NTH_VERSION, pyv,
                             os.getpid(), now_ts),
                        )
                    except (sqlite3.OperationalError, OSError):
                        # OSError: socket.gethostname() can fail in odd
                        # sandboxes, and fleet bookkeeping must never be
                        # the thing that kills the notification loop.
                        pass
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
                # DEPENDS ON nth_server.nth_ack being strictly either/or: a
                # session-token client's ack advances sessions.last_read and
                # NEVER members.last_read. So both must be reconciled here.
                # Do not drop the sessions read on the grounds that the
                # capability is agent-global — it is not, on this schema, and
                # removing it silently re-notifies every acknowledged message
                # at a cost scaling with messages x agents. It retires only
                # together with a sessions-global migration that makes ack
                # write members.last_read as well.
                # See tests/test-watermark-session-scope.py.
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

                # Pull the optional columns alongside the message, degrading
                # one at a time if the schema predates them. This used to nest
                # three try/excepts over refs and bangs only — `recipients`
                # was added to every tier INCLUDING the innermost, which has no
                # handler, so a database predating it did not degrade: the
                # OperationalError escaped and the monitor read no messages at
                # all, waking for nothing, silently. (Found by integrating the
                # DM-visibility filter with up/monitor's tests, whose fixture
                # schema has no recipients column — neither branch failed
                # alone.) Ordered richest-first; each entry drops one more.
                OPTIONAL_TIERS = (
                    ("refs", "bangs", "recipients"),
                    ("refs", "bangs"),            # pre-DM
                    ("refs", "recipients"),       # pre-v7.2 bangs
                    ("refs",),
                    ("recipients",),              # pre-v7.1 refs
                    (),                           # oldest schema we support
                )
                unread = None
                for _optional in OPTIONAL_TIERS:
                    cols = ["id", "mentions", *_optional,
                            "member_id", "member_name", "content"]
                    try:
                        unread = db.execute(
                            "SELECT " + ", ".join(cols) + " FROM messages "
                            "WHERE channel = ? AND id > ? AND member_id != ? "
                            "ORDER BY id",
                            (channel, local_hwm, member_id),
                        ).fetchall()
                        break
                    except sqlite3.OperationalError:
                        continue
                if unread is None:
                    # Every tier failed: the table is not one we recognise at
                    # all. Skip this tick rather than crash the monitor.
                    unread = []

                if unread:
                    # Advance the LOCAL watermark over the WHOLE raw batch
                    # first, so a DM this member cannot see still moves the
                    # cursor past it instead of re-surfacing on every tick.
                    local_hwm = max(m["id"] for m in unread)

                    # Then drop DMs this member is not a party to, BEFORE
                    # should_wake — otherwise the new_messages event carries a
                    # content preview of a DM addressed to someone else, which
                    # is itself the leak. The monitor only ever runs for an
                    # agent, so kind is 'agent'; a pre-migration row with no
                    # recipients is a broadcast, unchanged.
                    unread = [
                        m for m in unread
                        if can_see(member_id, "agent",
                                   (m["member_id"] if "member_id" in m.keys() else None),
                                   (m["recipients"] if "recipients" in m.keys() else ""),
                                   allow_all_seeing=False)
                    ]

                if unread:

                    mode = effective_mode if effective_mode in FILTER_MODES else "all"
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

                # --- Cadence + cache keepalive ---
                # Single query for this member's most-recent own message —
                # drives two independent gates:
                #
                #   * cadence (10min, active + claimed-task only): nudges a
                #     worker who holds a task and has gone silent.
                #
                #   * keepalive (55min, always): gives the parent session a
                #     cheap wake so it can tap the Anthropic prompt cache
                #     (1h TTL) with a single trio_poll before it expires —
                #     ~$0.13 vs ~$2.25 for a full context rewrite on the
                #     eventual real wake. Fires for every idle member,
                #     including hibernators, because the cache cost is
                #     paid on the parent session whether it's asleep or
                #     not and we want it cheap to re-engage.
                try:
                    latest_own = db.execute(
                        "SELECT created_at FROM messages "
                        "WHERE channel = ? AND member_id = ? ORDER BY id DESC LIMIT 1",
                        (channel, member_id),
                    ).fetchone()
                except sqlite3.OperationalError:
                    latest_own = None
                own_gap = seconds_since(
                    latest_own["created_at"] if latest_own else None
                )

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
                        if own_gap > CADENCE_THRESHOLD and not cadence_fired:
                            emit({"event": "cadence", "gap_seconds": gap_for_emit(own_gap), "claimed_tasks": claimed_count})
                            cadence_fired = True
                        elif own_gap < CADENCE_THRESHOLD:
                            cadence_fired = False
                    else:
                        cadence_fired = False
                else:
                    cadence_fired = False

                # Check how long since a peer engaged this specific agent
                # — @me, #me, !me, or one of the broadcast wildcards (@all,
                # !all, both of which expand to include every member's id
                # in the sigil arrays at send time). Plain channel chatter
                # that ignores us doesn't count: we're only worth keeping
                # warm if someone has actually been poking us recently.
                # LIKE on the quoted JSON token avoids needing json_extract
                # and matches "id1","id2" reliably because every entry is
                # double-quoted in the stored array.
                mid_token = f'%"{member_id}"%'
                try:
                    last_engaged = db.execute(
                        "SELECT created_at FROM messages "
                        "WHERE channel = ? AND member_id != ? "
                        "AND (mentions LIKE ? OR refs LIKE ? OR bangs LIKE ?) "
                        "ORDER BY id DESC LIMIT 1",
                        (channel, member_id, mid_token, mid_token, mid_token),
                    ).fetchone()
                except sqlite3.OperationalError:
                    last_engaged = None
                engaged_gap = seconds_since(
                    last_engaged["created_at"] if last_engaged else None
                )
                # The agent's own recent activity also counts as "needed"
                # — an agent actively working in the channel shouldn't be
                # culled from the keepalive loop. Use the smaller (= more
                # recent) of the two gaps.
                needed_gap = min(own_gap, engaged_gap)
                stale_in_channel = needed_gap > KEEPALIVE_GIVEUP

                if (own_gap > KEEPALIVE_THRESHOLD
                        and not stale_in_channel
                        and not keepalive_fired):
                    emit(build_keepalive_event(own_gap, engaged_gap))
                    keepalive_fired = True
                elif own_gap < KEEPALIVE_THRESHOLD:
                    keepalive_fired = False

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


def parse_session_token_arg(argv_tail):
    """Return the --session-token value, or "" when not supplied.

    Optional on purpose: without it the monitor keeps its historical behaviour
    of never exiting on session state. With it, the monitor stops when that
    exact token is revoked — the displaced half of a duplicate identity.
    """
    i = 0
    while i < len(argv_tail):
        if argv_tail[i] == "--session-token":
            if i + 1 >= len(argv_tail):
                raise ValueError("--session-token requires a value")
            return argv_tail[i + 1]
        i += 1
    return ""


if __name__ == "__main__":
    if len(sys.argv) < 3:
        emit({"event": "error",
              "msg": "Usage: nth_monitor.py <channel> <member_id> "
                     "[--filter all|about|at | --mention-filter] "
                     "[--session-token TOKEN]"})
        sys.exit(1)

    channel_arg = sys.argv[1]
    member_arg = sys.argv[2]
    try:
        filter_arg = parse_filter_arg(sys.argv[3:])
        token_arg = parse_session_token_arg(sys.argv[3:])
    except ValueError as e:
        emit({"event": "error", "msg": str(e)})
        sys.exit(1)

    try:
        monitor(channel_arg, member_arg, filter_mode=filter_arg,
                session_token=token_arg)
    except KeyboardInterrupt:
        pass
