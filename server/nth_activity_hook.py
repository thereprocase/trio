#!/usr/bin/env python3
"""nth_activity_hook.py — Claude Code PreToolUse/UserPromptSubmit hook for the
working indicator.

Wire this as BOTH a `PreToolUse` and a `UserPromptSubmit` hook in settings.json,
each MATCHER-LESS (fires for every tool / every prompt). Whenever a Claude
session does *anything* mid-turn — submits a prompt, or is about to run a tool
(Bash, Read, a sub-agent, a trio poll, ...) — Claude Code runs this script with
the hook payload on stdin and we stamp `sessions.last_seen` for the matching
session.

Why this exists
---------------
The dashboard's working/idle split is `sessions.last_seen > last_turn_end`
(see member_status in nth_web.py). Before this hook, `sessions.last_seen` was
bumped ONLY by trio MCP calls (poll/send/ack) — so an agent that was reasoning,
generating tokens, running a long `Bash`, or grinding through a sub-agent with
no trio chatter had a *stale* last_seen and read as **idle** even though it was
hard at work. The green dot only lit from the agent's first trio call in a turn
until its Stop hook fired.

This hook decouples "working" from trio-call cadence: any tool call keeps
last_seen fresh, so the dot stays green for the whole active turn. Because
PreToolUse fires at the *start* of each tool call, a long-running tool (a
multi-minute `Bash`, a sub-agent) keeps the session green for its full
duration. The turn's Stop hook (nth_turn_hook.py) stamps `last_turn_end`
*after* the last tool call, so the dot correctly flips to idle exactly when the
turn ends.

Why last_seen and not a new column
----------------------------------
`sessions.last_seen` already means "this session did something", and the roster
already derives liveness from it. Stamping it on every tool call makes that
literally true for the whole turn rather than only from the session's first trio
RPC, so a member reasoning or running a long Bash reads as working rather than
idle. A genuinely stalled turn runs no tools, so this cannot invent activity.

Design contract (same as nth_turn_hook.py):
  * PreToolUse fires *constantly*, so this must be dead-cheap: one UPDATE, no
    reads, no allocation beyond the payload parse.
  * Claude Code ignores a PreToolUse hook's stdout for scheduling as long as we
    exit 0 without emitting a decision, so there's nothing to return.
  * It must NEVER raise, hang, or disturb the host session. Every failure path
    is swallowed and we exit 0.

Mapping: the payload's `session_id` equals the connect-time
CLAUDE_CODE_SESSION_ID stored in sessions.fingerprint, so we update
WHERE fingerprint = session_id.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("NTH_DB_PATH", str(Path.home() / ".claude" / "nth" / "nth.db")))


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

    # Only act on activity events. The settings.json registration scopes this to
    # PreToolUse / UserPromptSubmit, but defend against a mis-wired hook.
    event = payload.get("hook_event_name")
    if event and event not in ("PreToolUse", "UserPromptSubmit"):
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
        conn = sqlite3.connect(str(DB_PATH), timeout=5, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE sessions SET last_seen = ? "
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
        conn.execute("COMMIT")
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
