#!/usr/bin/env python3
"""nth_supervisor lifecycle test — spawn / session-capture / feed / hibernate /
wake(resume) / stop — driven against tests/fake_agent.py so NO real billed
Claude session is ever launched.
"""
import collections
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))

# Point the supervisor at the fake stream-json agent BEFORE importing it isn't
# necessary (agent_binary reads the env at call time), but set it up front.
import os  # noqa: E402
os.environ["TRIO_AGENT_CMD"] = f"{sys.executable} {HERE / 'fake_agent.py'}"

import nth_supervisor as sup  # noqa: E402

failures = 0


def check(label, cond):
    global failures
    print(("PASS" if cond else "FAIL") + ": " + label)
    if not cond:
        failures += 1


AGENTS_DDL = """
CREATE TABLE agents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
  base_prompt TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'stopped',
  managed INTEGER NOT NULL DEFAULT 1, session_id TEXT, pid INTEGER, owner TEXT,
  effort TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, last_active_at TEXT);
"""


def main() -> int:
    import sqlite3
    tmp = Path(tempfile.mkdtemp(prefix="nth-sup-test-"))
    db_path = tmp / "nth.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(AGENTS_DDL)
    db.execute("INSERT INTO agents (id, name, model, created_at) "
               "VALUES ('ag1', 'Aragorn', 'sonnet', ?)", (sup.now_iso(),))
    db.commit()
    db.close()

    # Collect assistant echoes off the reader thread.
    echoes = []
    got_echo = threading.Event()

    def on_event(agent_id, evt):
        if evt.get("type") == "assistant":
            echoes.append(evt["message"]["content"])
            got_echo.set()

    # ── pure builders ──
    argv = sup.build_spawn_argv(model="sonnet", system_prompt="hi",
                                mcp_config="{}", resume_session_id="sX")
    check("build_spawn_argv: headless stream-json flags",
          "-p" in argv and "--input-format" in argv and "stream-json" in argv)
    # Permission mode comes from the agent's permission_profile; it is not a
    # fixed string. The default profile must map to a NON-blocking mode: a
    # headless agent has no terminal to answer an interactive prompt on, so a
    # blocking mode would freeze it until the approval inbox timed out.
    check("build_spawn_argv: default profile gives a non-blocking mode",
          "--permission-mode" in argv
          and argv[argv.index("--permission-mode") + 1]
          == sup.PERMISSION_MODES["balanced"])
    # build_spawn_argv takes the resolved MODE; the hub maps profile -> mode via
    # PERMISSION_MODES before calling. Pin every profile's mode so a change to
    # the mapping is a deliberate edit rather than a silent one — "autonomous"
    # in particular resolves to bypassPermissions, which disables the approval
    # gate entirely.
    check("PERMISSION_MODES covers exactly the three profiles",
          set(sup.PERMISSION_MODES) == {"observe", "balanced", "autonomous"})
    check("PERMISSION_MODES: autonomous is the only one that bypasses approval",
          [p for p, m in sup.PERMISSION_MODES.items()
           if m == "bypassPermissions"] == ["autonomous"])
    for _profile, _mode in sup.PERMISSION_MODES.items():
        _argv = sup.build_spawn_argv(model="sonnet", system_prompt="hi",
                                     mcp_config="{}", permission_mode=_mode)
        check(f"build_spawn_argv: profile {_profile!r} -> mode {_mode!r}",
              "--permission-mode" in _argv
              and _argv[_argv.index("--permission-mode") + 1] == _mode)
    check("build_spawn_argv: AskUserQuestion disallowed",
          "--disallowedTools" in argv
          and "AskUserQuestion" in argv[argv.index("--disallowedTools") + 1])
    check("build_spawn_argv: every Trio tool is pre-approved headlessly",
          "--allowedTools" in argv
          and "mcp__nth-trio__trio_dm" in argv[argv.index("--allowedTools") + 1]
          and "mcp__nth-trio__trio_ask" in argv[argv.index("--allowedTools") + 1])
    check("build_spawn_argv: model + resume + mcp + prompt passed",
          argv[argv.index("--model") + 1] == "sonnet"
          and argv[argv.index("--resume") + 1] == "sX"
          and "--mcp-config" in argv and "--append-system-prompt" in argv)
    check("build_spawn_argv: permission-prompt-tool wired when mcp_config present",
          "--permission-prompt-tool" in argv
          and argv[argv.index("--permission-prompt-tool") + 1] == sup.PERMISSION_PROMPT_TOOL)
    check("build_spawn_argv: permission-prompt-tool itself is model-disallowed",
          sup.PERMISSION_PROMPT_TOOL in argv[argv.index("--disallowedTools") + 1])

    argv_no_mcp = sup.build_spawn_argv(model="sonnet")
    check("build_spawn_argv: no permission-prompt-tool without an mcp_config",
          "--permission-prompt-tool" not in argv_no_mcp)

    argv_bypass = sup.build_spawn_argv(mcp_config="{}", permission_mode="bypassPermissions")
    check("build_spawn_argv: no permission-prompt-tool under bypassPermissions",
          "--permission-prompt-tool" not in argv_bypass)

    # LOTC/Aragorn: build_spawn_argv used to grant every agent --add-dir on
    # the WHOLE shared attachments root, letting any agent Read every other
    # channel's uploads. Must now grant nothing unless the caller explicitly
    # scopes it via extra_dirs (the caller is responsible for passing only
    # the channels this specific agent is actually a member of).
    argv_no_extra = sup.build_spawn_argv(mcp_config="{}")
    check("build_spawn_argv: NO --add-dir grant without explicit extra_dirs",
          "--add-dir" not in argv_no_extra)
    argv_scoped = sup.build_spawn_argv(mcp_config="{}", extra_dirs=["/tmp/x/chanA", "/tmp/x/chanB"])
    add_dirs = [argv_scoped[i + 1] for i, a in enumerate(argv_scoped) if a == "--add-dir"]
    check("build_spawn_argv: --add-dir grants EXACTLY the passed extra_dirs, nothing more",
          sorted(add_dirs) == ["/tmp/x/chanA", "/tmp/x/chanB"])

    import json as _json
    cfg = _json.loads(sup.build_mcp_config("/x/nth_server.py", python_cmd="py3"))
    check("build_mcp_config: registers nth-trio stdio server pointed at nth_server",
          cfg["mcpServers"]["nth-trio"]["command"] == "py3"
          and cfg["mcpServers"]["nth-trio"]["args"] == ["/x/nth_server.py"]
          and cfg["mcpServers"]["nth-trio"]["type"] == "stdio")

    runtime = sup.ClaudeRuntime()
    diag = runtime.diagnostics()
    check("runtime diagnostics recognize the configured fake agent",
          diag["provider"] == "claude" and diag["override"]
          and diag["available"] and diag["ready"])

    s = sup.AgentSupervisor(db_path=db_path, on_event=on_event, runtime=runtime)
    check("supervisor owns the explicit runtime adapter", s.runtime is runtime)

    # ── spawn ──
    proc = s.spawn("ag1", model="sonnet")
    check("spawn captures session_id from init", proc.session_id == "sess-fake-sonnet-001")
    check("spawn: process alive", proc.alive())
    row = _row(db_path, "ag1")
    check("spawn: db state=running", row["state"] == "running")
    check("spawn: db pid set", row["pid"] == proc.pid and proc.pid is not None)
    check("spawn: db session_id persisted", row["session_id"] == "sess-fake-sonnet-001")

    # ── feed (inbound routing, channel-tagged) ──
    ok = s.feed("ag1", "alpha", "hello there")
    got_echo.wait(3.0)
    check("feed: returns True", ok)
    check("feed: agent echoed the channel-tagged message",
          any("[#alpha] hello there" in e for e in echoes))

    # ── hibernate: process dies, session_id retained ──
    s.hibernate("ag1")
    time.sleep(0.2)
    row = _row(db_path, "ag1")
    check("hibernate: db state=sleeping", row["state"] == "sleeping")
    check("hibernate: pid cleared", row["pid"] is None)
    check("hibernate: session_id retained for resume", row["session_id"] == "sess-fake-sonnet-001")
    check("hibernate: process not alive", not proc.alive())
    check("hibernate: supervisor no longer lists it live", "ag1" not in s.live_ids())

    # ── wake: resume from the SAME session_id ──
    woke = s.wake("ag1")
    check("wake: returns a proc", woke is not None)
    check("wake: resumed the SAME session_id", woke and woke.session_id == "sess-fake-sonnet-001")
    row = _row(db_path, "ag1")
    check("wake: db state=running again", row["state"] == "running")

    # ── compact + clear context ──
    got_echo.clear()
    check("compact: sends Claude Code's /compact command", s.compact(
        "ag1", message="Keep the current plan"))
    got_echo.wait(3.0)
    check("compact: guidance reaches the live session",
          any("/compact Keep the current plan" in e for e in echoes))
    before_clear = woke
    fresh = s.clear("ag1", system_prompt="fresh", mcp_config="{}")
    check("clear: launches a replacement process", fresh is not None and fresh is not before_clear)
    check("clear: fresh launch does not pass --resume",
          fresh is not None and "--resume" not in fresh.argv)
    check("clear: replacement is live", fresh is not None and fresh.alive())

    # ── stop: terminal ──
    s.stop("ag1")
    time.sleep(0.2)
    row = _row(db_path, "ag1")
    check("stop: db state=stopped", row["state"] == "stopped")
    check("stop: pid cleared", row["pid"] is None)
    check("stop: not running", not s.is_running("ag1"))

    # ── non-dict JSON robustness (Uruk-Hai): reader must skip junk + still
    #    capture session_id, not crash the thread ──
    os.environ["FAKE_AGENT_PREJUNK"] = "1"
    db2 = sqlite3.connect(str(db_path))
    db2.execute("INSERT INTO agents (id, name, model, created_at) "
                "VALUES ('agjunk', 'Junk', 'sonnet', ?)", (sup.now_iso(),))
    db2.commit(); db2.close()
    pj = s.spawn("agjunk", model="sonnet")
    check("non-dict JSON lines skipped, session still captured",
          pj.session_id == "sess-fake-sonnet-001" and pj.alive())
    s.stop("agjunk")
    del os.environ["FAKE_AGENT_PREJUNK"]

    # ── errored spawn (Ents): process dies before init → state=errored, popped ──
    os.environ["FAKE_AGENT_CRASH"] = "1"
    db3 = sqlite3.connect(str(db_path))
    db3.execute("INSERT INTO agents (id, name, model, created_at) "
                "VALUES ('agbad', 'Bad', 'sonnet', ?)", (sup.now_iso(),))
    db3.commit(); db3.close()
    bad = s.spawn("agbad", model="sonnet", session_timeout=1.0)
    check("errored spawn: process not alive", not bad.alive())
    check("errored spawn: db state=errored", _row(db_path, "agbad")["state"] == "errored")
    check("errored spawn: dropped from registry (no zombie)", "agbad" not in s.live_ids())
    del os.environ["FAKE_AGENT_CRASH"]

    # ── concurrent spawn of same agent → exactly one process (Ents) ──
    db4 = sqlite3.connect(str(db_path))
    db4.execute("INSERT INTO agents (id, name, model, created_at) "
                "VALUES ('agconc', 'Conc', 'sonnet', ?)", (sup.now_iso(),))
    db4.commit(); db4.close()
    results = {}

    def race(n):
        results[n] = s.spawn("agconc", model="sonnet")
    t1 = threading.Thread(target=race, args=(1,))
    t2 = threading.Thread(target=race, args=(2,))
    t1.start(); t2.start(); t1.join(); t2.join()
    check("concurrent spawn dedup: both callers get the SAME proc",
          results[1] is results[2] and results[1].pid is not None)

    # ── feed to a process that died out-of-band → False, and reconcile flips
    #    the stale 'running' row (Ents/Legolas) ──
    s._procs["agconc"].proc.kill()
    time.sleep(0.2)
    check("feed to dead process returns False", s.feed("agconc", "alpha", "x") is False)

    # bugs/2026-08-01-claude-crash-retains-pending-context.md: a turn context
    # queued right before the crash must not survive reconcile() — otherwise
    # the next process's plain result pops this stale entry and bridges to
    # the DEAD turn's channel/baseline instead of its own.
    s._pending["agconc"] = collections.deque([
        {"channel": "alpha", "baseline": 0,
         "source_message_id": 1, "source_sender": "someone"}])
    reaped = s.reconcile()
    check("reconcile reaps the dead agent", "agconc" in reaped)
    check("reconcile flips stale running→errored", _row(db_path, "agconc")["state"] == "errored")
    check("reconcile clears the dead process's pending turn context",
          "agconc" not in s._pending)

    # ── Claude-side approval inbox (DB-backed; see nth_server.approvals) ──
    approvals_db = sqlite3.connect(str(db_path))
    approvals_db.executescript("""
        CREATE TABLE approvals (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'claude',
            tool_name TEXT NOT NULL DEFAULT '', tool_input TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', decision TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, resolved_at TEXT);
    """)
    approvals_db.execute(
        "INSERT INTO approvals (id, agent_id, agent_name, tool_name, status, created_at) "
        "VALUES ('cap_1', 'agconc', 'Concurrent', 'Bash', 'pending', ?)", (sup.now_iso(),))
    approvals_db.commit()
    approvals_db.close()
    check("pending_approvals: surfaces the pending row",
          [a["id"] for a in s.pending_approvals()] == ["cap_1"])
    check("resolve_approval: rejects an unknown decision word",
          s.resolve_approval("cap_1", "allow") is False)
    check("resolve_approval: accept resolves it",
          s.resolve_approval("cap_1", "accept") is True)
    check("resolve_approval: no longer pending after resolve",
          s.pending_approvals() == [])
    check("resolve_approval: re-resolving an already-resolved row is a no-op",
          s.resolve_approval("cap_1", "decline") is False)
    check("resolve_approval: unknown id is a no-op",
          s.resolve_approval("cap_missing", "accept") is False)

    # ── wake an agent that never spawned (no session_id) → cold start ──
    db5 = sqlite3.connect(str(db_path))
    db5.execute("INSERT INTO agents (id, name, model, created_at) "
                "VALUES ('agcold', 'Cold', 'haiku', ?)", (sup.now_iso(),))
    db5.commit(); db5.close()
    cold = s.wake("agcold")
    check("wake with no session_id: cold-starts a fresh session",
          cold is not None and cold.session_id == "sess-fake-haiku-001")
    check("wake nonexistent agent returns None", s.wake("nope") is None)
    s.stop("agcold")

    # ── shutdown with a LIVE agent actually stops it (Ents: prior test popped
    #    everything before shutdown, so its body never ran) ──
    db6 = sqlite3.connect(str(db_path))
    db6.execute("INSERT INTO agents (id, name, model, created_at) "
                "VALUES ('aglive', 'Live', 'sonnet', ?)", (sup.now_iso(),))
    db6.commit(); db6.close()
    live = s.spawn("aglive", model="sonnet")
    check("shutdown precondition: agent live", live.alive())
    s.shutdown()
    time.sleep(0.2)
    check("shutdown stops a live agent", not live.alive())
    check("shutdown marks it stopped", _row(db_path, "aglive")["state"] == "stopped")

    print(f"\n{'OK' if failures == 0 else 'FAILED'} — {failures} failure(s)")
    return 1 if failures else 0


def _row(db_path, agent_id):
    import sqlite3
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
