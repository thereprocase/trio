#!/usr/bin/env python3
"""nth_activity_hook.py — Claude Code activity hook for the working / tool-use /
blocked indicators.

Wire this MATCHER-LESS as a `PreToolUse`, `PostToolUse`, and `UserPromptSubmit`
hook in settings.json (fires for every tool / every prompt). Whenever a Claude
session does *anything* mid-turn — submits a prompt, is about to run a tool, or
finishes one — Claude Code runs this script with the hook payload on stdin. We
stamp `sessions.last_seen` for the matching session (the working indicator) and,
on `PreToolUse`, also record a SHORT summary of what tool is running (the
tool-use / sub-agent indicators) and flip the `blocked` flag for interactive
host prompts.

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

Three signals, one hook (they share this capture so the hot path opens once):
  * working — every event stamps `sessions.last_seen`.
  * tool-use — `PreToolUse` records `sessions.last_tool_name/last_tool_target`
    (the collapsed roster chip) and appends to a capped `tool_events` table (the
    expandable recent-calls list). A `Task` spawn is just a `PreToolUse` with
    tool_name `Task`; its `subagent_type`/`description` land in the same row, so
    the roster can surface spawned sub-agents too.
  * blocked — on `PreToolUse` for an interactive-blocking tool
    (`AskUserQuestion`, `ExitPlanMode`) we set `sessions.blocked_since`.
    member_status() renders `blocked` loudly.

    It is cleared by the matching tool's `PostToolUse` (the human answered), by
    a new prompt, and by the turn hook at every turn end — and by nothing else.
    In particular an ordinary tool does NOT clear it: Claude Code dispatches
    tools in parallel, so a sibling Read running while AskUserQuestion still
    waits on the human would clear the flag and the session would read as
    un-blocked for the rest of the prompt. One column cannot be both the signal
    and its own self-heal. The turn-end clear is what bounds a stale flag (an
    Esc-aborted prompt fires no PostToolUse) without racing the signal.

Why last_seen and not a new column
----------------------------------
`sessions.last_seen` already means "this session did something", and the roster
already derives liveness from it. Stamping it on every tool call makes that
literally true for the whole turn rather than only from the session's first trio
RPC, so a member reasoning or running a long Bash reads as working rather than
idle. A genuinely stalled turn runs no tools, so this cannot invent activity.

Privacy contract
----------------
We store a SUMMARY, never raw `tool_input` — inputs carry file contents, command
lines, URLs with tokens, secrets. Only a small whitelist of fields is read, each
capped, and never a value-bearing argument: Bash keeps the program name only
(never args/flags/env, which is where secrets live), file tools keep a basename,
Task keeps subagent_type + the agent-authored description, Glob/Grep keep the
(capped) pattern. Everything else — including every URL and search query —
stores the tool name alone.

The Bash summary is the sharp edge, because a shell command line is the most
likely place for a live credential. It is parsed with `shlex` (so a *quoted*
multi-word value stays one token and is skipped whole), refuses any command
containing a substitution construct (`$(…)`, backticks, `${…}`, process
substitution — there the real program is computed at runtime and the tokens
around it are fragments of a command we would be storing blind), and finally
accepts the result only if it looks like a plain program name. That last gate is
an allow-list on purpose: enumerating dangerous shell syntax is a losing game,
so anything not recognisably a command name is dropped rather than guessed at.

Accepted residual risks, all narrow and all requiring the secret to be in a
field a human or agent chose to name:
  * Glob/Grep patterns — an agent searching FOR a literal secret value surfaces
    it. Requires searching for secret-shaped text; capped short.
  * Task descriptions — agent-authored free text, stored up to 80 chars. An
    agent that pastes an error message or config fragment into a description
    leaks it. This is the same exposure as the agent simply saying it in the
    channel, which it can already do.
  * Filenames — a file literally named for its contents
    (`aws_key_AKIA….txt`) leaks through the basename.
None of these can be closed by parsing, only by storing less; the tool name
alone would be safer and much less useful. They are logged where a channel
member can see them, not published.

Performance contract (same as nth_turn_hook.py)
-----------------------------------------------
`PreToolUse` fires on the critical path of EVERY tool call and Claude Code
blocks the tool until this exits, so this must be dead-cheap:
  * A busy database is not a reason to delay the host session. This telemetry is
    best-effort and the next hook/MCP call refreshes it, so we fail FAST
    (HOOK_DB_TIMEOUT_S) rather than stall the host's tool under write
    contention. Measured: with a competing writer holding the lock for 1s, a
    50ms budget blocks the host ~80ms; a 500ms budget blocks it ~560ms.
  * ONE lock acquisition covering all writes. The tool_events insert + prune
    ride inside the same short transaction as the UPDATE rather than taking the
    write lock three separate times (which would triple the worst case).
  * Never reads beyond the payload parse, never allocates beyond it.
  * It must NEVER raise, hang, or disturb the host session — every failure path
    is swallowed and we exit 0.

Mapping: the payload's `session_id` equals the connect-time
CLAUDE_CODE_SESSION_ID stored in sessions.fingerprint, so we update
WHERE fingerprint = session_id.
"""
import json
import os
import re
import shlex
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# A leading `NAME=value` shell env-assignment (e.g. `AWS_SECRET=... aws ...`,
# `TOKEN=... curl ...`) — the most common way a secret rides on a command line.
# We skip these when picking the program name so a secret is never stored.
# Matched against shlex tokens, so a QUOTED value stays one token: naive
# whitespace splitting turned `API_KEY="hunter 2" curl` into
# ['API_KEY="hunter', '2"', 'curl'] and stored `2"` — half the secret — as the
# "program name".
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# What a program name may look like once basename'd. An ALLOW-list, deliberately:
# a deny-list of shell metacharacters is a losing game, and anything that isn't a
# plain command name is something we do not understand well enough to store.
_PROGRAM_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")

# Substitution constructs. shlex does not evaluate them, so `X=$(cat ~/.netrc) cmd`
# tokenizes as ['X=$(cat', '~/.netrc)', 'cmd'] and the "program name" would be a
# fragment of the substituted command. There is no safe summary of a command
# whose real program is computed at runtime, so we store nothing.
_SHELL_SUBSTITUTION = ("$(", "`", "${", "<(", ">(")

DB_PATH = Path(os.environ.get("NTH_DB_PATH", str(Path.home() / ".claude" / "nth" / "nth.db")))
HOOK_DB_TIMEOUT_S = 0.05

# Interactive host-native prompts that FREEZE the session until a human answers.
# PreToolUse fires as they start blocking; PostToolUse fires only once answered.
# Marking the member `blocked` on these makes a silently-stalled room loud.
BLOCKING_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Capped recent-calls ring, per session — the expandable list. Bounds the table
# to (live sessions x this) rows; the prune below enforces it on every insert.
TOOL_EVENTS_PER_SESSION = 20

_NAME_MAX = 40      # tool_name is a fixed vocabulary; cap only defends the row
_TARGET_MAX = 80    # short summary — a basename / program name / description head

# Scope every write to the NEWEST live session per channel for this fingerprint.
# A CLAUDE_CODE_SESSION_ID is not unique to a member: nth_connect mints a fresh
# member_id on every connect and never revokes the old row, so one fingerprint
# accumulates a row per reconnect. An unscoped UPDATE stamps them all,
# resurrecting long-dead members as "working" and corrupting
# effective_last_seen. Joining several channels from one session IS legitimate —
# one live member each — so scope per channel rather than to a single row.
#
# The inner pick is `ORDER BY ... LIMIT 1`, not `connected_at = MAX(...)`:
# two live rows in one channel sharing a connected_at timestamp both satisfy
# `= MAX(...)`, so a tie stamped every tied row — the very multi-row write this
# scope exists to prevent. Ties are not hypothetical: connect timestamps have
# whole-second resolution in some code paths. session_token breaks the tie so
# the choice is deterministic rather than merely single.
_LIVE_SESSION_SCOPE = (
    " WHERE fingerprint = ? AND revoked_at IS NULL"
    "   AND session_token IN ("
    "     SELECT s2.session_token FROM sessions s2"
    "      WHERE s2.fingerprint = ? AND s2.revoked_at IS NULL"
    "        AND s2.session_token = ("
    "          SELECT s3.session_token FROM sessions s3"
    "           WHERE s3.fingerprint = s2.fingerprint"
    "             AND s3.channel = s2.channel"
    "             AND s3.revoked_at IS NULL"
    "           ORDER BY s3.connected_at DESC, s3.session_token DESC"
    "           LIMIT 1))"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap(s: str, n: int = _TARGET_MAX) -> str:
    """Trim to a short, single-line summary. Never returns raw multi-line input."""
    if not s:
        return ""
    s = " ".join(str(s).split())   # collapse whitespace/newlines to one line
    return s[:n]


def _summarize_target(tool_name: str, tool_input) -> str:
    """A SHORT, privacy-safe target for the roster chip / recent-calls list.

    Reads only a whitelist of fields, never a value-bearing argument. See the
    privacy contract in the module docstring. Returns "" when there is nothing
    safe/useful to show (the chip then falls back to the bare tool name).
    """
    if not isinstance(tool_input, dict):
        return ""
    try:
        if tool_name == "Bash":
            # Program name ONLY. Args/flags come after the program and are where
            # secrets live (`mysql -pPASS`, `curl ...?token=`); leading
            # `NAME=value` env-assignments come BEFORE it and are also secret
            # carriers.
            cmd = tool_input.get("command")
            if not isinstance(cmd, str):
                return ""
            cmd = cmd.strip()
            if not cmd:
                return ""
            # Anything whose program is computed at runtime has no safe summary.
            if any(marker in cmd for marker in _SHELL_SUBSTITUTION):
                return ""
            try:
                # shlex, not str.split(): it honours quoting, so a quoted
                # multi-word secret stays inside its own token and is skipped
                # whole by _ENV_ASSIGN_RE instead of half-leaking.
                tokens = shlex.split(cmd)
            except ValueError:
                return ""   # unbalanced quotes — we cannot tokenize it safely
            head = ""
            for tok in tokens:
                if _ENV_ASSIGN_RE.match(tok):
                    continue  # env assignment — never the program, may be secret
                head = tok
                break
            name = os.path.basename(head)
            # Final gate: only a plain command name is stored. This is what
            # catches every shape the two rules above did not anticipate —
            # redirections, operators, a stray value fragment, non-ASCII.
            if not _PROGRAM_NAME_RE.match(name):
                return ""
            return _cap(name, 40)
        if tool_name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
            fp = (tool_input.get("file_path")
                  or tool_input.get("notebook_path") or "")
            return _cap(os.path.basename(fp))
        if tool_name in ("Glob", "Grep"):
            # A search pattern / path the agent chose — not file content.
            # Residual risk (accepted, documented): an agent grepping FOR a
            # literal secret value would surface it here. Narrow (requires
            # searching for secret-shaped text) and capped short to limit it.
            return _cap(tool_input.get("pattern") or tool_input.get("path") or "", 48)
        if tool_name in ("Task", "Agent"):
            st = _cap(tool_input.get("subagent_type") or "", 32)
            desc = tool_input.get("description") or ""
            both = (st + ": " + desc).strip(": ").strip() if st else desc
            return _cap(both)
        # WebFetch/WebSearch and everything else: no target. URLs and queries
        # carry tokens; the tool name alone is the safe signal.
    except Exception:
        return ""
    return ""


def _migrate(conn) -> None:
    """Fallback schema, for a hook upgraded ahead of its server.

    nth_server.py's get_db() owns the canonical schema and creates all of this
    on every connection, so in normal operation this never runs. It exists only
    for the window where a new hook is installed but the server has not been
    restarted — without it the hook would silently drop last_seen until then.

    It is deliberately reached only via the "no such column"/"no such table"
    exception path: this is DDL, and running it speculatively on every tool call
    would put schema work on the host's critical path."""
    for col in ("last_tool_name TEXT", "last_tool_target TEXT",
                "last_tool_at TEXT", "blocked_since TEXT"):
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # already exists
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tool_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " fingerprint TEXT NOT NULL,"
        " tool_name TEXT NOT NULL DEFAULT '',"
        " target TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_events_fingerprint "
        "ON tool_events (fingerprint, id)"
    )


def _apply(conn, event, session_id, tool_name, target, now) -> None:
    """One short transaction — a SINGLE write-lock acquisition covering the
    UPDATE plus, only for a tracked session on PreToolUse, the capped
    tool_events insert + prune. Taking the lock once and holding it briefly is
    cheaper for the host than three separate acquisitions, each of which could
    pay the full busy timeout."""
    fp = session_id[:64]
    blocking = tool_name in BLOCKING_TOOLS
    conn.execute("BEGIN IMMEDIATE")
    if event == "PreToolUse" and blocking:
        # Entering an interactive prompt. Set the flag alongside the usual
        # stamps.
        cur = conn.execute(
            "UPDATE sessions SET last_seen = ?, last_tool_name = ?, "
            "last_tool_target = ?, last_tool_at = ?, blocked_since = ?"
            + _LIVE_SESSION_SCOPE,
            (now, tool_name[:_NAME_MAX], target, now, now, fp, fp),
        )
    elif event == "PreToolUse" and tool_name:
        # An ordinary tool. Deliberately does NOT touch blocked_since.
        #
        # Clearing it here looked like a free self-heal, but Claude Code
        # dispatches tools in parallel: a sibling Read firing while
        # AskUserQuestion is still waiting on the human would clear the flag and
        # the session would read as un-blocked for the rest of the prompt. The
        # flag is a single slot, so it cannot serve as both the signal and its
        # own self-heal. The turn hook clears it at every turn end instead,
        # which bounds a stale flag to the current turn without racing.
        cur = conn.execute(
            "UPDATE sessions SET last_seen = ?, last_tool_name = ?, "
            "last_tool_target = ?, last_tool_at = ?"
            + _LIVE_SESSION_SCOPE,
            (now, tool_name[:_NAME_MAX], target, now, fp, fp),
        )
    elif event == "PreToolUse":
        # PreToolUse with no tool_name (a Claude Code build that omits the
        # field, or a mis-wired hook). Stamp liveness only — writing the tool
        # columns here would blank the roster chip on every such event.
        cur = conn.execute(
            "UPDATE sessions SET last_seen = ?" + _LIVE_SESSION_SCOPE,
            (now, fp, fp),
        )
    elif event == "PostToolUse" and blocking:
        # The human answered the interactive prompt. This is the ONLY event
        # that clears the flag mid-turn — matching it to the blocking tool is
        # what keeps a sibling tool's PostToolUse from clearing it early.
        cur = conn.execute(
            "UPDATE sessions SET last_seen = ?, blocked_since = NULL"
            + _LIVE_SESSION_SCOPE,
            (now, fp, fp),
        )
    elif event == "UserPromptSubmit":
        # A new prompt means the previous turn is over and nothing is waiting
        # on a human, so a leftover flag is definitely stale.
        cur = conn.execute(
            "UPDATE sessions SET last_seen = ?, blocked_since = NULL"
            + _LIVE_SESSION_SCOPE,
            (now, fp, fp),
        )
    else:
        # PostToolUse for an ordinary tool: liveness only. Leave last_tool_*
        # alone — it reflects the last tool that STARTED — and leave
        # blocked_since alone, per the parallel-dispatch note above.
        cur = conn.execute(
            "UPDATE sessions SET last_seen = ?" + _LIVE_SESSION_SCOPE,
            (now, fp, fp),
        )

    # Only record events for a session trio actually tracks (rowcount>0), so
    # the capped table can't fill with orphan sub-agent/unknown sessions.
    if event == "PreToolUse" and cur.rowcount and tool_name:
        conn.execute(
            "INSERT INTO tool_events (fingerprint, tool_name, target, created_at) "
            "VALUES (?, ?, ?, ?)",
            (fp, tool_name[:_NAME_MAX], target, now),
        )
        # Bounded prune: keep only the newest N rows for THIS fingerprint.
        conn.execute(
            "DELETE FROM tool_events WHERE fingerprint = ? AND id NOT IN "
            "(SELECT id FROM tool_events WHERE fingerprint = ? "
            " ORDER BY id DESC LIMIT ?)",
            (fp, fp, TOOL_EVENTS_PER_SESSION),
        )
    conn.execute("COMMIT")


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

    # Only act on activity events. The settings.json registration scopes this,
    # but defend against a mis-wired hook. A truly absent field (None) is
    # tolerated (some Claude Code versions omit it and the registration already
    # scopes us) and treated as PreToolUse-equivalent; any *present* value that
    # isn't ours (including "" or "Stop") is rejected.
    event = payload.get("hook_event_name")
    if event not in (None, "PreToolUse", "PostToolUse", "UserPromptSubmit"):
        return 0
    if event is None:
        event = "PreToolUse"

    session_id = (payload.get("session_id")
                  or os.environ.get("CLAUDE_CODE_SESSION_ID")
                  or os.environ.get("CLAUDE_SESSION_ID")
                  or "")
    if not session_id:
        return 0

    # tool_name is needed on PostToolUse too, not just PreToolUse: clearing
    # blocked_since is scoped to the *matching* tool, so we have to know which
    # tool just finished.
    tool_name = ""
    target = ""
    if event in ("PreToolUse", "PostToolUse"):
        tn = payload.get("tool_name")
        tool_name = tn if isinstance(tn, str) else ""
    if event == "PreToolUse":
        # Only PreToolUse summarises the input — PostToolUse carries a result,
        # which we never read.
        target = _summarize_target(tool_name, payload.get("tool_input"))

    # A pure-telemetry hook must not materialise a database. sqlite3.connect
    # creates the file, so without this a stray NTH_DB_PATH (or a hook running
    # before the server has ever started) leaves behind a DB containing only
    # tool_events — which the real server would then never adopt.
    if not DB_PATH.exists():
        return 0

    now = _now_iso()
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=HOOK_DB_TIMEOUT_S,
                               isolation_level=None)
        # Every other DB-touching module in this codebase runs NORMAL under WAL.
        # This is the hottest writer of them all: FULL fsyncs on every commit,
        # i.e. on every tool call of every session.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={int(HOOK_DB_TIMEOUT_S * 1000)}")
        try:
            _apply(conn, event, session_id, tool_name, target, now)
        except sqlite3.OperationalError as e:
            # Distinguish a SCHEMA mismatch (missing column/table — the
            # transitional case _migrate handles) from a LOCK/BUSY timeout,
            # which raises the SAME exception type. A busy timeout is exactly
            # the contention this hook must fail FAST on — migrating + retrying
            # there would trade the fast give-up for a DDL+retry storm under the
            # very load we're protecting. So only migrate on a genuine schema
            # error; otherwise give up (the next tool call re-stamps).
            msg = str(e).lower()
            if "no such column" not in msg and "no such table" not in msg:
                return 0  # locked/busy — fail fast, don't compound contention
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            _migrate(conn)
            _apply(conn, event, session_id, tool_name, target, now)
    except Exception:
        return 0  # best-effort: never disturb the host session
    finally:
        if conn is not None:
            # Explicit: every give-up path above can leave an open transaction,
            # and close() only rolls it back as an implementation detail of
            # CPython's sqlite3. Keep the invariant local.
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
