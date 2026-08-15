"""Tests for the tool-use / blocked signals the activity hook records.

test-working-indicator-activity.py covers the `last_seen` stamping and the
robustness contract. This file covers what the hook records *about* the tool:

  * privacy — `_summarize_target` never stores a value-bearing argument
  * the roster chip — sessions.last_tool_name / last_tool_target / last_tool_at
  * the recent-calls ring — tool_events, capped per session, orphans rejected
  * the blocked flag — set by an interactive tool, cleared by anything else
  * scoping — tool columns land only on the newest live session, like last_seen
  * the performance contract — the hook gives up rather than blocking the host
    session behind another writer

Usage: python tests/test-tool-activity.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_activity_hook as hook   # noqa: E402  (pure helpers only)
import nth_server as srv           # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


# ---------------------------------------------------------------- privacy ---
# Pure-function tests: no DB, no subprocess. These encode the privacy contract
# in the module docstring — a secret must never reach the database.

def t_privacy():
    s = hook._summarize_target

    # A leading NAME=value env assignment is the most common way a secret rides
    # on a command line. The program name is what we want, never the assignment.
    check("privacy: bash env-assignment skipped, program kept",
          s("Bash", {"command": "AWS_SECRET_ACCESS_KEY=abc123 aws s3 ls"}) == "aws")
    check("privacy: several env assignments skipped",
          s("Bash", {"command": "A=1 B=2 curl https://x/?token=SECRET"}) == "curl")
    # Args are where the rest of the secrets live — only the program survives.
    check("privacy: bash args dropped",
          s("Bash", {"command": "mysql -pHUNTER2 -h db.internal"}) == "mysql")
    check("privacy: bash keeps basename, not the full path",
          s("Bash", {"command": "/usr/local/bin/psql --password=x"}) == "psql")
    # Belt and braces: anything still shaped like a value stores nothing.
    check("privacy: value-shaped head stores nothing",
          s("Bash", {"command": "=weird"}) == "")
    check("privacy: empty command is empty",
          s("Bash", {"command": "   "}) == "")

    check("privacy: file tools keep a basename only",
          s("Read", {"file_path": "/Users/me/secrets/prod.env"}) == "prod.env")
    check("privacy: notebook_path also honoured",
          s("NotebookEdit", {"notebook_path": "/a/b/c.ipynb"}) == "c.ipynb")

    check("privacy: Task keeps subagent_type + description",
          s("Task", {"subagent_type": "ent", "description": "triage the diff"})
          == "ent: triage the diff")
    check("privacy: Task with no subagent_type keeps description",
          s("Task", {"description": "just this"}) == "just this")

    # URLs and search queries carry tokens — the tool name alone is the signal.
    check("privacy: WebFetch stores no target",
          s("WebFetch", {"url": "https://x/?token=SECRET"}) == "")
    check("privacy: unknown tool stores no target",
          s("Frobnicate", {"anything": "at all"}) == "")

    check("privacy: non-dict tool_input is safe",
          s("Bash", "rm -rf /") == "" and s("Bash", None) == "")

    # Summaries are single-line and capped — never a blob of file content.
    long_desc = "x" * 500
    out = s("Task", {"description": long_desc})
    check("privacy: summary is capped", len(out) <= hook._TARGET_MAX)
    multiline = s("Glob", {"pattern": "a\nb\nc"})
    check("privacy: newlines collapsed to one line", "\n" not in multiline)


# ------------------------------------------------------------------- DB -----

_tmp = tempfile.mkdtemp(prefix="nth_tool_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"
DB = str(srv.DB_PATH)

os.environ["CLAUDE_CODE_SESSION_ID"] = "tool-sid-1"
_r = json.loads(srv.nth_connect(summary="t", name="Worker", channel="tool1"))
CH, AGENT = _r["channel"], _r["member_id"]
SID = "tool-sid-1"


def raw():
    c = sqlite3.connect(DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def fire(payload, db=None, timeout=30):
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    env = {**os.environ, "NTH_DB_PATH": db or DB}
    p = subprocess.run([sys.executable, str(SERVER / "nth_activity_hook.py")],
                       input=payload, text=True, capture_output=True,
                       env=env, timeout=timeout)
    return p.returncode


def pre(tool_name, tool_input=None, session_id=SID):
    return fire({"session_id": session_id, "hook_event_name": "PreToolUse",
                 "tool_name": tool_name, "tool_input": tool_input or {}})


def session_row(session_id=SID):
    c = raw()
    try:
        return c.execute(
            "SELECT * FROM sessions WHERE fingerprint=? ORDER BY connected_at DESC",
            (session_id,)).fetchone()
    finally:
        c.close()


def tool_events(session_id=SID):
    c = raw()
    try:
        return c.execute(
            "SELECT tool_name, target FROM tool_events WHERE session_id=? "
            "ORDER BY id", (session_id,)).fetchall()
    finally:
        c.close()


def t_roster_chip():
    pre("Bash", {"command": "rg --hidden pattern"})
    row = session_row()
    check("chip: last_tool_name recorded", row["last_tool_name"] == "Bash")
    check("chip: last_tool_target is the program name", row["last_tool_target"] == "rg")
    check("chip: last_tool_at stamped", bool(row["last_tool_at"]))
    check("chip: last_seen stamped in the same write",
          row["last_seen"] == row["last_tool_at"])

    pre("Read", {"file_path": "/tmp/notes.md"})
    row = session_row()
    check("chip: overwritten by the next tool",
          row["last_tool_name"] == "Read" and row["last_tool_target"] == "notes.md")


def t_ring_is_capped():
    before = len(tool_events())
    n = hook.TOOL_EVENTS_PER_SESSION + 12
    for i in range(n):
        pre("Bash", {"command": f"echo {i}"})
    rows = tool_events()
    check(f"ring: capped at {hook.TOOL_EVENTS_PER_SESSION} rows per session",
          len(rows) <= hook.TOOL_EVENTS_PER_SESSION)
    check("ring: keeps the NEWEST events, not the oldest",
          rows[-1]["tool_name"] == "Bash" and len(rows) > 0)
    check("ring: pruning actually ran (did not just grow)",
          len(rows) < before + n)


def t_orphan_sessions_rejected():
    """A sub-agent or unknown session has no sessions row. Its events must not
    accumulate in the capped table — the prune is per-session, so orphans would
    grow without bound."""
    before = len(tool_events("no-such-session"))
    for _ in range(5):
        pre("Bash", {"command": "echo hi"}, session_id="no-such-session")
    after = len(tool_events("no-such-session"))
    check("orphan: untracked session records no tool_events",
          before == 0 and after == 0)


def t_blocked_flag():
    pre("AskUserQuestion", {"question": "which?"})
    check("blocked: interactive tool sets blocked_since",
          bool(session_row()["blocked_since"]))

    pre("ExitPlanMode", {})
    check("blocked: ExitPlanMode also blocks",
          bool(session_row()["blocked_since"]))

    pre("Bash", {"command": "ls"})
    check("blocked: an ordinary tool clears it",
          session_row()["blocked_since"] is None)

    # PostToolUse (the human answered) clears it even if PreToolUse set it.
    pre("AskUserQuestion", {"question": "again?"})
    fire({"session_id": SID, "hook_event_name": "PostToolUse",
          "tool_name": "AskUserQuestion"})
    check("blocked: PostToolUse clears it",
          session_row()["blocked_since"] is None)

    pre("AskUserQuestion", {"question": "third?"})
    fire({"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    check("blocked: a new prompt clears it",
          session_row()["blocked_since"] is None)


def t_scoping():
    """The tool columns must follow the same newest-live-session-per-channel
    scoping as last_seen. One fingerprint accumulates a sessions row per
    reconnect; stamping them all resurrects dead members as 'working'."""
    c = raw()
    try:
        c.execute(
            "INSERT INTO sessions (session_token, member_id, channel, fingerprint,"
            " connected_at, last_seen, role) VALUES (?,?,?,?,?,?,?)",
            ("stale-tok", "m-stale", CH, SID, iso(-9999), "", "member"))
    finally:
        c.close()

    pre("Grep", {"pattern": "needle"})

    c = raw()
    try:
        stale = c.execute(
            "SELECT last_tool_name, last_seen FROM sessions WHERE session_token=?",
            ("stale-tok",)).fetchone()
        live = c.execute(
            "SELECT last_tool_name FROM sessions WHERE fingerprint=? "
            "AND session_token != 'stale-tok' ORDER BY connected_at DESC",
            (SID,)).fetchone()
    finally:
        c.close()

    check("scoping: the stale reconnect row is NOT stamped",
          not stale["last_tool_name"] and not stale["last_seen"])
    check("scoping: the newest live row IS stamped",
          live["last_tool_name"] == "Grep")


def t_fails_fast_under_contention():
    """The performance contract: PreToolUse sits on the critical path of every
    tool call, so a busy database must not stall the host. The hook must give up
    on its short budget rather than wait out another writer's lock."""
    tmp = tempfile.mkdtemp(prefix="nth_tool_lock_")
    db = str(Path(tmp) / "lock.db")
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE sessions (session_token TEXT PRIMARY KEY, member_id TEXT,"
        " channel TEXT, fingerprint TEXT, connected_at TEXT, last_seen TEXT,"
        " last_tool_name TEXT, last_tool_target TEXT, last_tool_at TEXT,"
        " blocked_since TEXT, revoked_at TEXT);")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("INSERT INTO sessions (session_token, fingerprint, channel,"
              " connected_at, last_seen) VALUES ('t','lock-sid','c','2026-01-01','')")
    c.commit()
    c.close()

    HOLD = 4.0
    ready = threading.Event()

    def holder():
        h = sqlite3.connect(db, timeout=30, isolation_level=None)
        h.execute("PRAGMA busy_timeout=30000")
        h.execute("BEGIN IMMEDIATE")
        h.execute("UPDATE sessions SET last_seen='held' WHERE session_token='t'")
        ready.set()
        time.sleep(HOLD)
        h.execute("COMMIT")
        h.close()

    payload = {"session_id": "lock-sid", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": "ls"}}

    # Baseline on the SAME db with no contention. Subtracting it normalises out
    # interpreter start-up, which dwarfs the DB work and varies by machine —
    # without this the assertion is either flaky or too loose to catch anything.
    fire(payload, db=db)                      # warm
    t0 = time.perf_counter()
    fire(payload, db=db)
    uncontended = time.perf_counter() - t0

    t = threading.Thread(target=holder)
    t.start()
    ready.wait()
    t0 = time.perf_counter()
    rc = fire(payload, db=db)
    contended = time.perf_counter() - t0
    t.join()

    added = max(0.0, contended - uncontended)
    # The budget is HOOK_DB_TIMEOUT_S. Allow a generous multiple for SQLite's
    # busy-handler granularity, but far below HOLD — waiting out the lock, or
    # the old 500ms budget, both land well outside this.
    bound = max(0.35, hook.HOOK_DB_TIMEOUT_S * 6)
    check(f"perf: hook gave up rather than waiting out a {HOLD}s lock "
          f"(added {added*1000:.0f}ms over baseline, bound {bound*1000:.0f}ms)",
          added < bound)
    check("perf: exits 0 even when it gave up", rc == 0)


t_privacy()
t_roster_chip()
t_ring_is_capped()
t_orphan_sessions_rejected()
t_blocked_flag()
t_scoping()
t_fails_fast_under_contention()

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")
