#!/usr/bin/env python3
"""nth_turn_hook.py — Claude Code Stop/StopFailure hook for the working indicator.

Wire this as BOTH a `Stop` and a `StopFailure` hook in settings.json — the
StopFailure registration is MATCHER-LESS (fires on every error type), because
this hook must record *every* turn end so a frozen/stalled turn can't leave a
session reading as a false "working". Whenever a Claude turn ends — cleanly
(Stop) or on an API error (StopFailure) — Claude Code runs this script with the
hook payload on stdin. We stamp `sessions.last_turn_end`
for the matching session so the dashboard can distinguish:
  * "working" — the agent has acted (its own tool calls bump sessions.last_seen)
    MORE recently than its last turn end -> it woke and is mid-turn.
  * "idle"    — the last thing that happened is a turn end -> done / waiting.

Design contract (same as nth_activity_hook.py):
  * This hook only UPDATEs one timestamp. Claude Code ignores a Stop/StopFailure
    hook's output/exit code, so there's nothing to return.
  * It must NEVER raise, hang, or disturb the host session. Every failure path is
    swallowed and we exit 0.

It records turn ends for BOTH Stop and StopFailure so a *stalled* turn — one
that ends in an error rather than cleanly — still counts as "ended" here. A
frozen session must not read as a false "working".

Mapping: the payload's `session_id` equals the connect-time CLAUDE_CODE_SESSION_ID
stored in sessions.fingerprint, so we update WHERE fingerprint = session_id.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("NTH_DB_PATH", str(Path.home() / ".claude" / "nth" / "nth.db")))
HOOK_DB_TIMEOUT_S = 0.05


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
    if not isinstance(payload, dict):
        return 0

    # Only act on turn-end events. The settings.json registration scopes this to
    # Stop / StopFailure, but defend against a mis-wired hook pointing here.
    event = payload.get("hook_event_name")
    if event and event not in ("Stop", "StopFailure"):
        return 0

    session_id = (payload.get("session_id")
                  or os.environ.get("CLAUDE_CODE_SESSION_ID")
                  or os.environ.get("CLAUDE_SESSION_ID")
                  or "")
    if not session_id:
        return 0

    now = _now_iso()
    conn = None
    try:
        # A Stop hook is still on the host's critical path. Its timestamp is
        # best-effort telemetry, so skip a busy database rather than making a
        # completed turn wait behind another writer. The single UPDATE is atomic
        # in autocommit mode; an explicit BEGIN IMMEDIATE only adds contention.
        conn = sqlite3.connect(str(DB_PATH), timeout=HOOK_DB_TIMEOUT_S,
                               isolation_level=None)
        conn.execute("PRAGMA busy_timeout=50")
        conn.execute(
                # blocked_since is cleared here as well as stamped: it marks a
                # session frozen on an interactive prompt (nth_activity_hook),
                # and a prompt the user aborts with Esc fires no PostToolUse, so
                # nothing else would clear it. A turn end always happens, and by
                # definition nothing is waiting on a human once the turn is
                # over — so this bounds a stale flag to the turn that set it,
                # which is exactly the idle stretch where the indicator matters.
                "UPDATE sessions SET last_turn_end = ?, blocked_since = NULL "
                " WHERE fingerprint = ? AND revoked_at IS NULL"
                # Scope to the NEWEST live session per channel for this fingerprint.
                # A CLAUDE_CODE_SESSION_ID is not unique to a member: nth_connect
                # mints a fresh member_id on every connect and never revokes the old
                # row, so one fingerprint accumulates a row per reconnect. An
                # unscoped UPDATE stamps them all, resurrecting long-dead members as
                # "working" and corrupting effective_last_seen. Joining several
                # channels from one session IS legitimate — one live member each —
                # so scope per channel rather than to a single row.
                "  AND session_token IN ("
                "    SELECT s2.session_token FROM sessions s2"
                "     WHERE s2.fingerprint = ? AND s2.revoked_at IS NULL"
                "       AND s2.connected_at = ("
                "         SELECT MAX(s3.connected_at) FROM sessions s3"
                "          WHERE s3.fingerprint = s2.fingerprint"
                "            AND s3.channel = s2.channel"
                "            AND s3.revoked_at IS NULL))"
                ,
                (now, session_id[:64], session_id[:64]),
        )
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
