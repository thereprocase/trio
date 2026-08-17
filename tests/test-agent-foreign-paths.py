"""The paths that decide whether a duplicate agent gets created.

test-agent-ownership.py proves the primitives. This proves the three callers
that actually run in production, none of which were covered when the ownership
fix first landed — and they are the three the incident report names:

  * AgentRouter._worker_loop — the trigger. A hub that never spawned an agent
    sees every message to it as a cold start, so this is where the duplicate
    was actually born.
  * resume_managed_agents — fires on every hub restart, including a single hub
    restarting into its own SIGTERM-orphaned agents.
  * the destructive paths (stop/hibernate/interrupt/clear) — which null
    agents.pid, the sole cross-process ownership record. Guarding creation
    while leaving destruction open lets the NEXT spawn create the duplicate,
    so these matter as much as spawn() does.

Usage: python tests/test-agent-foreign-paths.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

_tmp = Path(tempfile.mkdtemp(prefix="nth_foreign_"))
os.environ["NTH_HOME"] = str(_tmp)
# Never let a regression here launch a REAL claude process. Without this,
# agent_binary() defaults to ["claude"], so a broken guard would spawn a live
# billed agent instead of failing the test.
os.environ["TRIO_AGENT_CMD"] = f"{sys.executable} {HERE / 'fake_agent.py'}"

import nth_supervisor as nsup                      # noqa: E402

try:
    import nth_web as nw                           # noqa: E402
except ImportError as exc:
    print(f"SKIP: nth_web import failed ({exc})")
    sys.exit(0)

failures = []
_spawned = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


AGENT = "ag_foreign_paths"


def live_agent_process(agent_id):
    """A process that looks like agent_id to pid_owns_agent."""
    marker = nsup.AGENT_ID_MARKER.format(agent_id=agent_id)
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time # {marker}\ntime.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _spawned.append(proc)
    for _ in range(50):
        if nsup._pid_cmdline(proc.pid):
            break
        time.sleep(0.05)
    return proc


def make_db(path, pid):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT DEFAULT '',"
               " model TEXT DEFAULT '', base_prompt TEXT DEFAULT '',"
               " state TEXT DEFAULT 'running', managed INTEGER DEFAULT 1,"
               " session_id TEXT, pid INTEGER, effort TEXT DEFAULT '',"
               " cwd TEXT DEFAULT '', permission_profile TEXT DEFAULT 'balanced',"
               " archived_at TEXT, last_active_at TEXT)")
    db.execute("CREATE TABLE agent_channels (agent_id TEXT, channel TEXT)")
    db.execute("INSERT INTO agents (id, pid, state) VALUES (?,?,'running')",
               (AGENT, pid))
    db.execute("INSERT INTO agent_channels VALUES (?, 'somechan')", (AGENT,))
    db.commit()
    db.close()


try:
    foreign = live_agent_process(AGENT)
    db_path = _tmp / "nth.db"
    make_db(db_path, foreign.pid)
    sup = nsup.AgentSupervisor(db_path=db_path)

    # ── the marker must be what identifies an agent, not a bare substring ──
    # An operator prompt that merely NAMES another agent must not make this
    # process answer to that agent's id.
    bystander = subprocess.Popen(
        [sys.executable, "-c",
         "import time # please coordinate with ag_someone_else\ntime.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _spawned.append(bystander)
    for _ in range(50):
        if nsup._pid_cmdline(bystander.pid):
            break
        time.sleep(0.05)
    check("a process that merely mentions an agent id does not own it",
          nsup.pid_owns_agent(bystander.pid, "ag_someone_else") is False)
    check("the marker phrase does identify its own agent",
          nsup.pid_owns_agent(foreign.pid, AGENT) is True)

    # ── resume_managed_agents must not revive a live agent ────────────────
    woke = []
    real_wake = nw.wake_agent
    nw.wake_agent = lambda aid, s, p: woke.append(aid)
    try:
        resumed = nw.resume_managed_agents(db_path, sup)
    finally:
        nw.wake_agent = real_wake
    check("resume_managed_agents does not resume a live foreign agent",
          resumed == [] and woke == [])

    row = sqlite3.connect(str(db_path)).execute(
        "SELECT state, pid FROM agents WHERE id=?", (AGENT,)).fetchone()
    # The dangerous failure isn't "it resumed" — it's "it marked the live
    # agent ERRORED and nulled the pid", which the router then skips forever
    # and which erases the ownership record entirely.
    check("resume leaves the live agent's state and pid untouched",
          row[0] == "running" and row[1] == foreign.pid)

    # ── the router must skip, not wake ────────────────────────────────────
    class FakeSup:
        def __init__(self, owner):
            self.owner = owner
            self.fed = []

        def is_running(self, aid):
            return False            # never ours: we are the second hub

        def foreign_owner_pid(self, aid):
            return self.owner

        def feed(self, *a, **kw):
            self.fed.append(a)
            return True

    for owner, label, expect_wake in (
            (foreign.pid, "owned by another hub", False),
            (None, "genuinely dead", True)):
        fake = FakeSup(owner)
        router = nw.AgentRouter(db_path, fake)
        woke = []
        nw.wake_agent = lambda aid, s, p: woke.append(aid)
        try:
            router._q.put((AGENT, "somechan", "hello", None, 1, "someone"))
            router._stop_event.clear()
            import threading
            t = threading.Thread(target=router._worker_loop, daemon=True)
            t.start()
            time.sleep(0.6)
            router._stop_event.set()
            t.join(timeout=2)
        finally:
            nw.wake_agent = real_wake
        check(f"router {'skips' if not expect_wake else 'wakes'} an agent "
              f"{label}", bool(woke) is expect_wake)
        if not expect_wake:
            check("router does not feed an agent it does not own",
                  fake.fed == [])

    # ── destructive paths must refuse, and must not touch the row ─────────
    for name, call in (
            ("stop", lambda: sup.stop(AGENT)),
            ("hibernate", lambda: sup.hibernate(AGENT)),
            ("interrupt", lambda: sup.interrupt(AGENT)),
            ("clear", lambda: sup.clear(AGENT))):
        try:
            call()
            check(f"{name}() refuses to act on a foreign-owned agent", False)
        except nsup.ForeignAgentError:
            check(f"{name}() refuses to act on a foreign-owned agent", True)
        except Exception as exc:                    # noqa: BLE001
            check(f"{name}() refuses to act on a foreign-owned agent "
                  f"(raised {type(exc).__name__})", False)

    row = sqlite3.connect(str(db_path)).execute(
        "SELECT state, pid, session_id FROM agents WHERE id=?",
        (AGENT,)).fetchone()
    check("no destructive path erased the pid that proves ownership",
          row[1] == foreign.pid)
    check("no destructive path parked the live agent in a dead state",
          row[0] == "running")

    # ── and once the owner really is gone, the agent is reclaimable ───────
    # The guard must not become a trap: an agent whose process died has to be
    # spawnable again, or the fix strands every agent it protects.
    foreign.kill()
    foreign.wait()
    sup._forget_owner(AGENT)
    check("a departed owner releases the agent for this hub to take",
          sup.foreign_owner_pid(AGENT) is None)
    try:
        sup.stop(AGENT)
        check("stop() works normally once the foreign process is gone", True)
    except Exception as exc:                        # noqa: BLE001
        check(f"stop() works normally once the foreign process is gone "
              f"(raised {type(exc).__name__})", False)

finally:
    for proc in _spawned:
        try:
            proc.kill()
        except Exception:
            pass

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all foreign-path checks passed")
