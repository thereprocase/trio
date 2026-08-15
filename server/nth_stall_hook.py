#!/usr/bin/env python3
"""nth_stall_hook.py — Claude Code StopFailure hook for the trio stall-watchdog.

Wire this as a `StopFailure` hook in settings.json. When a Claude session's
turn is terminated by an API error (overloaded / rate_limit / server_error /
...), Claude Code runs this script with the hook payload on stdin. We record
one row in the trio `stall_events` table; the watchdog (in nth_web.py) picks
it up, maps the session back to its trio member, and nudges it back to life.

Design contract:
  * This hook only ever INSERTs a row. Claude Code ignores a StopFailure
    hook's stdout/stderr/exit code, so there is nothing to "return" — all
    policy (mapping, backoff, retract, give-up) lives in the watchdog.
  * It must NEVER raise, hang, or otherwise disturb the host session. Every
    failure path is swallowed and we exit 0. A watchdog that breaks the thing
    it protects is worse than no watchdog.

Why this hook's DB budget differs from nth_activity_hook / nth_turn_hook
------------------------------------------------------------------------
Those two fire on the critical path — PreToolUse blocks a tool the agent is
actively waiting on — and the data they write is self-healing: a dropped
`last_seen` stamp is corrected by the very next tool call. So they fail fast
(50ms) and accept a miss.

A stall event is neither. StopFailure fires *after* the turn has already died,
so blocking here delays nothing the agent is waiting on. And the row is
one-shot: if we drop it, no later event re-reports the stall and the watchdog
never nudges — the session stays dead. Failing fast would trade the exact
reliability this hook exists to provide for latency nobody is waiting on.

So we take a middle budget: long enough to ride out ordinary write contention
(the monitor's 10s heartbeat, another session's hook), short enough not to
freeze a session for seconds during the case that actually stresses this path —
an API outage, where every session in the room fires StopFailure at once.
Measured with a competing writer holding the lock for 1s: the previous 5s
budget blocked for the full second and would have blocked for up to five; this
one gives up at ~2s worst case and in practice returns as soon as the lock
clears.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("NTH_DB_PATH", str(Path.home() / ".claude" / "nth" / "nth.db")))

# See "Why this hook's DB budget differs" above. Not the 50ms of the hot-path
# hooks, and not the 5s that let an outage storm freeze every session.
HOOK_DB_TIMEOUT_S = 2.0

# Mirrors nth_server.get_db()'s stall_events DDL so a stall is never dropped
# just because the server process hasn't initialized the schema yet. (The rest
# of this codebase already mirrors DDL across nth_server / nth_web.) Applied
# only when the INSERT reports the table missing — running DDL on every
# invocation costs a schema check on a path that fires during an outage storm.
_DDL = """
CREATE TABLE IF NOT EXISTS stall_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT NOT NULL,
    error              TEXT NOT NULL DEFAULT '',
    cwd                TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    resolved_at        TEXT,
    resolution         TEXT NOT NULL DEFAULT '',
    nudge_count        INTEGER NOT NULL DEFAULT 0,
    last_nudge_at      TEXT,
    last_nudge_msg_id  INTEGER
)
"""

_INSERT = (
    "INSERT INTO stall_events (session_id, error, cwd, created_at) "
    "VALUES (?, ?, ?, ?)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    try:
        raw = sys.stdin.read(1_000_000)   # bounded — never buffer a hostile stream
    except Exception:
        return 0
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    # Valid JSON that isn't an object (null / list / number / string) has no
    # fields to read — bail before any .get() so we honor the never-raise
    # contract.
    if not isinstance(payload, dict):
        return 0

    # Only act on StopFailure. The settings.json matcher should already scope
    # this, but defend against a mis-wired Stop hook pointing here.
    if payload.get("hook_event_name") and payload.get("hook_event_name") != "StopFailure":
        return 0

    # session_id is the mapping key. Fall back to the env var (same value in
    # practice) if the payload somehow lacks it.
    session_id = (payload.get("session_id")
                  or os.environ.get("CLAUDE_CODE_SESSION_ID")
                  or os.environ.get("CLAUDE_SESSION_ID")
                  or "")
    if not session_id:
        return 0  # nothing the watchdog could map — drop silently

    error = payload.get("error", "") or ""
    cwd = payload.get("cwd", "") or ""
    row = (session_id[:64], str(error)[:64], str(cwd)[:1024], _now_iso())

    conn = None
    try:
        # Autocommit: a single INSERT is already atomic, so there is no reason
        # to take an explicit write lock in advance.
        conn = sqlite3.connect(str(DB_PATH), timeout=HOOK_DB_TIMEOUT_S,
                               isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={int(HOOK_DB_TIMEOUT_S * 1000)}")
        try:
            conn.execute(_INSERT, row)
        except sqlite3.OperationalError as e:
            # Only a MISSING TABLE is worth recovering from here. A lock/busy
            # timeout raises the same exception type, and running DDL then would
            # compound the contention we just failed on.
            if "no such table" not in str(e).lower():
                return 0
            conn.execute(_DDL)
            conn.execute(_INSERT, row)
    except Exception:
        return 0  # best-effort: never disturb the host session
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
