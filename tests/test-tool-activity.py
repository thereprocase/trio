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
            "SELECT tool_name, target FROM tool_events WHERE fingerprint=? "
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
    cap = hook.TOOL_EVENTS_PER_SESSION
    n = cap + 12
    # Each event carries a DISTINCT target, so the assertions below can tell
    # which events survived. Using the same tool_name for all of them (as an
    # earlier version did) made the ordering check a tautology: it passed even
    # with the prune inverted to keep the OLDEST rows.
    for i in range(n):
        pre("Bash", {"command": f"echo{i}"})
    rows = tool_events()
    targets = [r["target"] for r in rows]

    check(f"ring: capped at {cap} rows per session", len(rows) <= cap)
    check("ring: newest event is the last one written",
          targets and targets[-1] == f"echo{n - 1}")
    check("ring: oldest events were dropped, not the newest",
          f"echo0" not in targets and f"echo{n - 1}" in targets)
    check("ring: the surviving window is exactly the newest `cap` events",
          targets == [f"echo{i}" for i in range(n - cap, n)])


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

    # THE parallel-dispatch case. Claude Code runs tools concurrently, so an
    # ordinary tool firing while the prompt is still open must NOT clear the
    # flag — otherwise a background Read un-blocks a session that is in fact
    # still waiting on a human, for the rest of the prompt.
    pre("Bash", {"command": "ls"})
    check("blocked: a sibling tool does NOT clear it (parallel dispatch)",
          bool(session_row()["blocked_since"]))
    pre("Read", {"file_path": "/tmp/x.md"})
    check("blocked: still set after a second sibling tool",
          bool(session_row()["blocked_since"]))

    # ...and the chip still tracks the most recent tool while blocked.
    check("blocked: the roster chip still follows the latest tool",
          session_row()["last_tool_name"] == "Read")

    # The matching tool's PostToolUse — the human answered — clears it.
    fire({"session_id": SID, "hook_event_name": "PostToolUse",
          "tool_name": "ExitPlanMode"})
    check("blocked: PostToolUse of the blocking tool clears it",
          session_row()["blocked_since"] is None)

    # A sibling tool's PostToolUse must not clear it either.
    pre("AskUserQuestion", {"question": "again?"})
    fire({"session_id": SID, "hook_event_name": "PostToolUse",
          "tool_name": "Bash"})
    check("blocked: PostToolUse of a SIBLING tool does not clear it",
          bool(session_row()["blocked_since"]))
    fire({"session_id": SID, "hook_event_name": "PostToolUse",
          "tool_name": "AskUserQuestion"})
    check("blocked: ...but its own PostToolUse does",
          session_row()["blocked_since"] is None)

    pre("AskUserQuestion", {"question": "third?"})
    fire({"session_id": SID, "hook_event_name": "UserPromptSubmit"})
    check("blocked: a new prompt clears it",
          session_row()["blocked_since"] is None)

    # An Esc-aborted prompt fires no PostToolUse at all. The turn hook is what
    # stops that flag from surviving into the idle stretch after the turn.
    pre("AskUserQuestion", {"question": "aborted?"})
    check("blocked: set before the abort", bool(session_row()["blocked_since"]))
    subprocess.run([sys.executable, str(SERVER / "nth_turn_hook.py")],
                   input=json.dumps({"session_id": SID,
                                     "hook_event_name": "Stop"}),
                   text=True, capture_output=True,
                   env={**os.environ, "NTH_DB_PATH": DB})
    check("blocked: the turn hook clears an abandoned block at turn end",
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


def t_scoping_edge_cases():
    """The three cases the scope exists for, beyond a plain stale reconnect:
    a NEWER but revoked row, one fingerprint live in several channels, and a
    connected_at TIE. The tie is the one a `connected_at = MAX(...)` scope gets
    wrong — every tied row satisfies it, which is the multi-row write the whole
    fragment is there to prevent."""
    sid = "scope-edge-sid"
    c = raw()
    try:
        rows = [
            # (token, channel, connected_at, revoked_at) — newest per channel
            # is what must be stamped.
            ("e-old-a",     "chA", iso(-500),  None),
            ("e-live-a",    "chA", iso(-100),  None),
            ("e-revoked-a", "chA", iso(-10),   iso(-5)),   # NEWEST but revoked
            ("e-live-b",    "chB", iso(-100),  None),      # other channel
            # Two live rows in one channel sharing a timestamp.
            ("e-tie-1",     "chC", "2026-08-14T00:00:00+00:00", None),
            ("e-tie-2",     "chC", "2026-08-14T00:00:00+00:00", None),
        ]
        for tok, ch, conn_at, revoked in rows:
            c.execute(
                "INSERT INTO sessions (session_token, member_id, channel,"
                " fingerprint, connected_at, last_seen, role, revoked_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tok, "m-" + tok, ch, sid, conn_at, "", "member", revoked))
    finally:
        c.close()

    pre("Glob", {"pattern": "*.py"}, session_id=sid)

    c = raw()
    try:
        got = {r["session_token"]: r["last_tool_name"] for r in c.execute(
            "SELECT session_token, last_tool_name FROM sessions "
            "WHERE fingerprint=?", (sid,)).fetchall()}
    finally:
        c.close()

    check("scoping: a NEWER but revoked row is not stamped",
          not got["e-revoked-a"])
    check("scoping: the newest LIVE row in that channel is stamped instead",
          got["e-live-a"] == "Glob")
    check("scoping: the older row in that channel is untouched",
          not got["e-old-a"])
    check("scoping: a second channel's live row is stamped too "
          "(joining several channels from one session is legitimate)",
          got["e-live-b"] == "Glob")
    tied = [got["e-tie-1"], got["e-tie-2"]]
    check("scoping: a connected_at TIE stamps exactly one row, not both",
          sum(1 for v in tied if v == "Glob") == 1)


def t_migrate_fallback():
    """A hook upgraded ahead of its server: the DB has none of the columns or
    the tool_events table. The hook must self-heal and land the write, rather
    than silently dropping activity until the server restarts."""
    tmp = tempfile.mkdtemp(prefix="nth_tool_migrate_")
    db = str(Path(tmp) / "old.db")
    c = sqlite3.connect(db)
    # Deliberately the PRE-feature schema: no last_tool_*, no blocked_since,
    # no tool_events.
    c.executescript(
        "CREATE TABLE sessions (session_token TEXT PRIMARY KEY, member_id TEXT,"
        " channel TEXT, fingerprint TEXT, connected_at TEXT, last_seen TEXT,"
        " revoked_at TEXT);")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("INSERT INTO sessions (session_token, fingerprint, channel,"
              " connected_at, last_seen) VALUES ('t','old-sid','c','2026-01-01','')")
    c.commit()
    c.close()

    rc = fire({"session_id": "old-sid", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": "ls"}}, db=db)
    check("migrate: exits 0", rc == 0)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute("SELECT * FROM sessions WHERE fingerprint='old-sid'").fetchone()
        keys = row.keys()
        events = c.execute(
            "SELECT tool_name, target FROM tool_events WHERE fingerprint='old-sid'"
        ).fetchall()
    finally:
        c.close()

    check("migrate: the missing columns were added",
          "last_tool_name" in keys and "blocked_since" in keys)
    check("migrate: the write landed after self-healing",
          row["last_tool_name"] == "Bash" and row["last_tool_target"] == "ls")
    check("migrate: tool_events was created and written",
          len(events) == 1 and events[0]["target"] == "ls")


def _legacy_db(prefix):
    """A DB whose tool_events is the PRE-fingerprint shape: keyed on
    `session_id TEXT NOT NULL`, exactly as installs that ran an earlier hooks
    build have it on disk."""
    tmp = tempfile.mkdtemp(prefix=prefix)
    db = str(Path(tmp) / "legacy.db")
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(
        "CREATE TABLE sessions (session_token TEXT PRIMARY KEY, member_id TEXT,"
        " channel TEXT, fingerprint TEXT, connected_at TEXT, last_seen TEXT,"
        " revoked_at TEXT, last_tool_name TEXT, last_tool_target TEXT,"
        " last_tool_at TEXT, blocked_since TEXT);"
        "CREATE TABLE tool_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, tool_name TEXT NOT NULL DEFAULT '',"
        " target TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);")
    c.execute("INSERT INTO sessions (session_token, fingerprint, channel,"
              " connected_at, last_seen) VALUES ('t','legacy-sid','c','2026-01-01','')")
    c.execute("INSERT INTO tool_events (session_id, tool_name, target, created_at)"
              " VALUES ('legacy-sid','Read','old.md','2026-01-01')")
    c.commit()
    c.close()
    return db


def t_legacy_ring_rebuilt_by_server():
    """The legacy table cannot simply gain a `fingerprint` column: `session_id`
    is NOT NULL with no default, so every insert naming only the canonical
    columns dies on a constraint. get_db() must REBUILD the table, carrying the
    old fingerprints across."""
    db = _legacy_db("nth_tool_legacy_")
    keep_path, keep_dir = srv.DB_PATH, srv.DB_DIR
    srv.DB_PATH, srv.DB_DIR = Path(db), Path(db).parent
    try:
        conn = srv.get_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tool_events)")]
        rows = conn.execute("SELECT fingerprint, target FROM tool_events").fetchall()
        conn.close()
    finally:
        srv.DB_PATH, srv.DB_DIR = keep_path, keep_dir

    check("legacy ring: the vestigial session_id column is gone",
          "session_id" not in cols and "fingerprint" in cols)
    check("legacy ring: existing rows survived the rebuild",
          len(rows) == 1 and rows[0]["target"] == "old.md")
    check("legacy ring: session_id was carried across as the fingerprint",
          rows and rows[0]["fingerprint"] == "legacy-sid")

    # The whole point of the rebuild: the hook's insert now works.
    rc = fire({"session_id": "legacy-sid", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": "ls"}}, db=db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute("SELECT * FROM sessions WHERE fingerprint='legacy-sid'").fetchone()
        n = c.execute("SELECT count(*) FROM tool_events").fetchone()[0]
    finally:
        c.close()
    check("legacy ring: after the rebuild the hook stamps and records",
          rc == 0 and row["last_tool_name"] == "Bash" and n == 2)


def t_stamp_survives_a_failing_ring_insert():
    """The regression this pair of fixes exists for. Against a legacy table the
    ring insert raises IntegrityError — which is NOT the OperationalError the
    hook handles, so it escaped and aborted the shared transaction, discarding
    the sessions UPDATE with it. Every upgraded install therefore reported a
    working agent as idle, forever. The ring is subordinate: losing an event is
    acceptable, losing the status is not."""
    db = _legacy_db("nth_tool_ring_fail_")
    rc = fire({"session_id": "legacy-sid", "hook_event_name": "PreToolUse",
               "tool_name": "Grep", "tool_input": {"pattern": "x"}}, db=db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute("SELECT * FROM sessions WHERE fingerprint='legacy-sid'").fetchone()
        cols = [r[1] for r in c.execute("PRAGMA table_info(tool_events)")]
    finally:
        c.close()

    # Precondition — if the hook ever migrates the table itself this test stops
    # exercising the failure it was written for, and must be rewritten.
    check("ring failure: the table under test really is still the legacy one",
          "session_id" in cols)
    check("ring failure: exits 0", rc == 0)
    check("ring failure: the status stamp still landed",
          row["last_tool_name"] == "Grep" and bool(row["last_tool_at"]))
    check("ring failure: liveness still landed",
          bool(row["last_seen"]))


def t_migrate_not_triggered_by_contention():
    """A busy timeout raises the SAME OperationalError as a schema mismatch.
    The hook must not confuse them: running DDL and retrying under write
    contention is exactly the storm the fast-fail budget exists to avoid."""
    tmp = tempfile.mkdtemp(prefix="nth_tool_migbusy_")
    db = str(Path(tmp) / "busy.db")
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE sessions (session_token TEXT PRIMARY KEY, member_id TEXT,"
        " channel TEXT, fingerprint TEXT, connected_at TEXT, last_seen TEXT,"
        " last_tool_name TEXT, last_tool_target TEXT, last_tool_at TEXT,"
        " blocked_since TEXT, revoked_at TEXT);")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("INSERT INTO sessions (session_token, fingerprint, channel,"
              " connected_at, last_seen) VALUES ('t','busy-sid','c','2026-01-01','')")
    c.commit()
    c.close()

    HOLD = 3.0
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

    t = threading.Thread(target=holder)
    t.start()
    ready.wait()
    t0 = time.perf_counter()
    rc = fire({"session_id": "busy-sid", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": "ls"}}, db=db)
    elapsed = time.perf_counter() - t0
    t.join()

    check("migrate: a busy timeout is not mistaken for a schema error "
          f"(gave up in {elapsed*1000:.0f}ms, lock held {HOLD}s)",
          elapsed < HOLD / 2)
    check("migrate: still exits 0 under contention", rc == 0)


def t_roster_consumes_it():
    """The payoff. The hook is only worth its hot-path cost if something reads
    what it writes — so assert the roster actually surfaces the tool chip and
    the blocked state, and that name/target come from the SAME write."""
    import nth_web as web

    hub = web.EventHub(srv.DB_PATH, CH)

    def roster_for(mid):
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        try:
            for m in hub._fetch_roster(c):
                if m["id"] == mid:
                    return m
        finally:
            c.close()
        return None

    pre("Bash", {"command": "rg --files"})
    m = roster_for(AGENT)
    check("roster: exposes the running tool",
          m is not None and m["last_tool_name"] == "Bash")
    check("roster: exposes the tool target", m["last_tool_target"] == "rg")
    check("roster: name and target are from the same write",
          bool(m["last_tool_at"]))

    pre("AskUserQuestion", {"question": "which?"})
    m = roster_for(AGENT)
    check("roster: a session waiting on a human reads 'blocked'",
          m["status"] == "blocked")
    check("roster: blocked_since is surfaced", bool(m["blocked_since"]))

    # ...and a sibling tool must not knock it out of blocked, end to end.
    pre("Read", {"file_path": "/tmp/z.md"})
    m = roster_for(AGENT)
    check("roster: still blocked while a sibling tool runs",
          m["status"] == "blocked")
    check("roster: but the chip followed the sibling tool",
          m["last_tool_name"] == "Read")

    fire({"session_id": SID, "hook_event_name": "PostToolUse",
          "tool_name": "AskUserQuestion"})
    m = roster_for(AGENT)
    check("roster: leaves blocked once the human answers",
          m["status"] != "blocked")


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
    # FIXED ceiling, deliberately not derived from HOOK_DB_TIMEOUT_S. Deriving
    # the bound from the constant it is meant to guard makes the test vacuous:
    # raising the constant raises the bound with it, so the 500ms budget this
    # hook used to have passed a HOOK_DB_TIMEOUT_S-derived bound cleanly.
    #
    # 300ms is the contract: a tool call may not be delayed by more than this
    # because another writer holds the DB. It is ~4x the measured ~75ms and
    # well under the old 500ms budget, so it catches a real regression without
    # being tight enough to flake on a loaded runner.
    BOUND = 0.30
    check(f"perf: hook gave up rather than waiting out a {HOLD}s lock "
          f"(added {added*1000:.0f}ms over baseline, bound {BOUND*1000:.0f}ms)",
          added < BOUND)
    check("perf: exits 0 even when it gave up", rc == 0)
    # Guard the constant directly too, so a regression is caught by a plain
    # assertion rather than only by a timing measurement.
    check("perf: the documented fast-fail budget has not been widened",
          hook.HOOK_DB_TIMEOUT_S <= 0.1)


t_privacy()
t_roster_chip()
t_ring_is_capped()
t_orphan_sessions_rejected()
t_blocked_flag()
t_scoping()
t_scoping_edge_cases()
t_migrate_fallback()
t_legacy_ring_rebuilt_by_server()
t_stamp_survives_a_failing_ring_insert()
t_migrate_not_triggered_by_contention()
t_roster_consumes_it()
t_fails_fast_under_contention()

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")
