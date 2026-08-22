#!/usr/bin/env python3
"""Regression: a managed agent's status must not depend on it calling connect.

The activity/turn hooks find a session by FINGERPRINT (the raw Claude Code
session id) and stamp last_seen / last_tool_* / last_turn_end on it. Every
derived status the roster shows — working, idle, the tool chip — reads those
columns.

Until _register_sessions existed, the ONLY thing that ever inserted a sessions
row was the agent choosing to call trio_connect. An agent is handed its member
id at spawn, so posting with trio_send and never connecting is legal and
happens — and such an agent had no row for the hooks to write to. It reported
whatever its member row last said, forever. Observed live on two agents created
ninety seconds apart: the one that connected reported correctly, the one that
did not read idle for its entire life.

So: the supervisor registers the rows itself, at the instant it captures the
fingerprint.

Usage: python tests/test-agent-session-anchor.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

import nth_supervisor as sup  # noqa: E402

failed = []


def check(label, ok):
    print(("PASS" if ok else "FAIL") + ": " + label)
    if not ok:
        failed.append(label)


TMP = tempfile.mkdtemp(prefix="nth_anchor_")
DB = Path(TMP) / "nth.db"
FP = "fingerprint-abc"
AGENT = "ag_test1"


def fresh_db():
    if DB.exists():
        DB.unlink()
    c = sqlite3.connect(str(DB))
    c.executescript(
        "CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT, session_id TEXT,"
        " runtime_ref TEXT, last_active_at TEXT);"
        "CREATE TABLE members (id TEXT, channel TEXT, name TEXT, active INTEGER"
        " NOT NULL DEFAULT 1, last_read INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE sessions (session_token TEXT PRIMARY KEY, member_id TEXT,"
        " channel TEXT, role TEXT, pid INTEGER, fingerprint TEXT,"
        " connected_at TEXT, last_seen TEXT, last_read INTEGER NOT NULL DEFAULT 0,"
        " revoked_at TEXT);")
    c.execute("INSERT INTO agents (id, name) VALUES (?, 'Lark')", (AGENT,))
    # Two placements, exactly as the agent manager creates them, plus a
    # watermark to prove the anchor row does not rewind it.
    c.execute("INSERT INTO members (id, channel, name, last_read)"
              " VALUES (?, 'work', 'Lark', 42)", (AGENT,))
    c.execute("INSERT INTO members (id, channel, name, last_read)"
              " VALUES (?, 'nth-agent-inbox', 'Lark', 7)", (AGENT,))
    c.commit()
    c.close()


def rows():
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    try:
        return c.execute(
            "SELECT * FROM sessions WHERE member_id = ? ORDER BY channel",
            (AGENT,)).fetchall()
    finally:
        c.close()


def persist(fingerprint=FP):
    sv = sup.AgentSupervisor(db_path=DB)
    sv._persist_session(AGENT, fingerprint)


def t_registers_every_placement():
    fresh_db()
    check("precondition: the agent has no sessions row", len(rows()) == 0)
    persist()
    r = rows()
    check("anchor: one row per channel placement", len(r) == 2)
    check("anchor: channels are the agent's placements",
          [x["channel"] for x in r] == ["nth-agent-inbox", "work"])
    check("anchor: rows carry the captured fingerprint",
          all(x["fingerprint"] == FP for x in r))
    check("anchor: rows are live", all(x["revoked_at"] is None for x in r))
    check("anchor: connected_at and last_seen are stamped",
          all(x["connected_at"] and x["last_seen"] for x in r))
    # Seeding at 0 would advertise the whole channel as unread for a session
    # that has in fact read up to the member watermark.
    check("anchor: last_read is seeded from the member watermark, not 0",
          {x["channel"]: x["last_read"] for x in r}
          == {"work": 42, "nth-agent-inbox": 7})


def t_the_hook_can_now_find_it():
    """The point of the row. Fire the real activity hook at the fingerprint and
    assert the stamp lands — this is the end-to-end contract, not a shape test."""
    fresh_db()
    persist()
    import json
    import subprocess
    payload = json.dumps({"session_id": FP, "hook_event_name": "PreToolUse",
                          "tool_name": "Bash", "tool_input": {"command": "ls"}})
    p = subprocess.run([sys.executable, str(SERVER / "nth_activity_hook.py")],
                       input=payload, text=True, capture_output=True,
                       env={**os.environ, "NTH_DB_PATH": str(DB)}, timeout=30)
    check("hook: exits 0", p.returncode == 0)
    r = rows()
    check("hook: the stamp landed on the anchored sessions",
          all(x["last_tool_name"] == "Bash" for x in r) and len(r) == 2)


def t_is_idempotent():
    """spawn -> hibernate -> revive re-captures the same fingerprint. A second
    row per channel would be harmless for the newest-wins scope but would grow
    the table on every restart."""
    fresh_db()
    persist()
    persist()
    persist()
    check("idempotent: re-capturing the same fingerprint adds no rows",
          len(rows()) == 2)


def t_a_new_fingerprint_gets_its_own_row():
    """--resume yields a NEW session id. The hooks fire on that one, so it needs
    a row; the scope takes the newest per channel, so the old row goes quiet on
    its own rather than needing a revoke."""
    fresh_db()
    persist()
    persist("fingerprint-second")
    r = rows()
    check("new fingerprint: a row was added for it", len(r) == 4)
    check("new fingerprint: both fingerprints are present",
          {x["fingerprint"] for x in r} == {FP, "fingerprint-second"})


def t_never_breaks_a_spawn():
    """Telemetry is subordinate to the process starting. An empty fingerprint
    and a hostile schema must both be no-ops, not exceptions."""
    fresh_db()
    persist("")
    check("robust: an empty fingerprint registers nothing", len(rows()) == 0)

    c = sqlite3.connect(str(DB))
    c.execute("DROP TABLE sessions")
    c.commit()
    c.close()
    try:
        persist()
        ok = True
    except Exception as e:                                   # noqa: BLE001
        ok = False
        print(f"  raised: {type(e).__name__}: {e}")
    check("robust: a missing sessions table does not raise", ok)


t_registers_every_placement()
t_the_hook_can_now_find_it()
t_is_idempotent()
t_a_new_fingerprint_gets_its_own_row()
t_never_breaks_a_spawn()

print()
if failed:
    print(f"FAILED — {len(failed)} failure(s)")
    for f in failed:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")
