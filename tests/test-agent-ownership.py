"""One agent id must never name two live processes, even across hubs.

Every liveness check on the spawn path reads AgentSupervisor._procs, which is
memory belonging to ONE hub process. Two nth_web instances against the same
database therefore each start with an empty registry, each conclude the agent
is dead, and each spawn it: two live processes sharing one member_id and one
channel identity. Observed in the field as duplicate Frost and Atlas agents,
each pair split across two hubs.

The database already recorded the owning pid; nothing consulted it. These are
the checks that now do, and the two properties they have to hold at once:

  * a live process elsewhere BLOCKS a spawn (no duplicate identities), and
  * a stale row does NOT block one (a recycled or dead pid must not strand an
    agent as permanently unspawnable).

The second is why bare liveness is not the test. Pids get recycled, so the
check also demands the agent's own id in the process's argv — every agent
carries it in the preamble baked into its command line.

Usage: python tests/test-agent-ownership.py
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

_tmp = Path(tempfile.mkdtemp(prefix="nth_owner_"))
os.environ["NTH_HOME"] = str(_tmp)
# agent_binary() defaults to the real `claude` CLI. This file's whole job is
# proving spawn() REFUSES, so if that guard ever regresses the test must fail
# — not launch a live, billed agent in whatever environment runs the suite.
os.environ["TRIO_AGENT_CMD"] = f"{sys.executable} {HERE / 'fake_agent.py'}"

import nth_supervisor as nsup    # noqa: E402

failures = []
_spawned = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def live_process_named(agent_id):
    """A real process carrying the preamble marker, as a real agent does."""
    marker = nsup.AGENT_ID_MARKER.format(agent_id=agent_id)
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import time # {marker}\ntime.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _spawned.append(proc)
    # ps has to be able to see it before any assertion about it is meaningful.
    for _ in range(50):
        if nsup._pid_cmdline(proc.pid):
            break
        time.sleep(0.05)
    return proc


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def make_db(path, rows):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, pid INTEGER, "
               "state TEXT NOT NULL DEFAULT 'stopped')")
    db.executemany("INSERT INTO agents (id, pid, state) VALUES (?,?,?)", rows)
    db.commit()
    db.close()


AGENT = "ag_ownership_test"
OTHER = "ag_someone_else"

# ── pid_owns_agent: the truth table ────────────────────────────────────────
mine = live_process_named(AGENT)

check("live pid whose argv carries the agent id is owned",
      nsup.pid_owns_agent(mine.pid, AGENT) is True)

check("live pid belonging to a DIFFERENT agent is not owned",
      nsup.pid_owns_agent(mine.pid, OTHER) is False)

check("dead pid is not owned",
      nsup.pid_owns_agent(dead_pid(), AGENT) is False)

# pid 1 is always alive and is definitively not one of our agents. This is the
# recycled-pid case: liveness alone would call it owned and strand the agent.
check("live pid that is not an agent at all is not owned (pid recycling)",
      nsup.pid_owns_agent(1, AGENT) is False)

for empty in (None, 0, -1):
    check(f"pid {empty!r} is not owned",
          nsup.pid_owns_agent(empty, AGENT) is False)

# os.kill(0, 0) SUCCEEDS — it signals the caller's own process group — so a
# NULL pid coerced to 0 would read as "alive" forever and whatever it guards
# could never be reclaimed.
check("pid_alive(0) is False despite os.kill(0,0) succeeding",
      nsup.pid_alive(0) is False)

# The documented safety property: when the command line can't be read at all,
# a live pid resolves to OWNED, because refusing a spawn is recoverable and a
# duplicate identity is not. Nothing else in this file exercises it, so a
# mutation flipping it to False would otherwise pass.
_real_cmdline = nsup._pid_cmdline
nsup._pid_cmdline = lambda pid: ""
try:
    check("an unreadable command line resolves to owned, not unowned",
          nsup.pid_owns_agent(mine.pid, AGENT) is True)
finally:
    nsup._pid_cmdline = _real_cmdline

# ── foreign_owner_pid: what the supervisor concludes from a row ────────────
db_path = _tmp / "nth.db"
make_db(db_path, [
    (AGENT, mine.pid, "running"),
    ("ag_stale", dead_pid(), "running"),
    ("ag_nullpid", None, "running"),
])
sup = nsup.AgentSupervisor(db_path=db_path)

check("row naming a live foreign process reports that pid",
      sup.foreign_owner_pid(AGENT) == mine.pid)

check("stale row (pid died with its hub) reports no owner",
      sup.foreign_owner_pid("ag_stale") is None)

check("row with no pid reports no owner",
      sup.foreign_owner_pid("ag_nullpid") is None)

check("agent with no row at all reports no owner",
      sup.foreign_owner_pid("ag_does_not_exist") is None)

# ── spawn() refuses rather than duplicating ────────────────────────────────
try:
    sup.spawn(AGENT)
    check("spawn() refuses when another hub owns a live process", False)
except nsup.ForeignAgentError as exc:
    check("spawn() refuses when another hub owns a live process",
          exc.pid == mine.pid and exc.agent_id == AGENT)
except Exception as exc:                    # noqa: BLE001 — wrong failure mode
    check(f"spawn() refuses when another hub owns a live process "
          f"(raised {type(exc).__name__} instead)", False)

# is_running_or_starting gates reclaim_secret rotation. Rotating a secret out
# from under a process we don't own leaves it holding a credential the database
# no longer has — B1, with a second hub cast as the racing thread.
check("is_running_or_starting() counts a foreign live process as running",
      sup.is_running_or_starting(AGENT) is True)

check("is_running() still answers only for processes THIS hub owns",
      sup.is_running(AGENT) is False)

check("is_running_or_starting() is False for a stale row",
      sup.is_running_or_starting("ag_stale") is False)

# ── a database that can't be read must degrade, not wedge every spawn ──────
broken = nsup.AgentSupervisor(db_path=_tmp / "does_not_exist_dir" / "x.db")
try:
    check("unreadable db reports no owner rather than raising",
          broken.foreign_owner_pid(AGENT) is None)
except Exception as exc:                    # noqa: BLE001
    check(f"unreadable db reports no owner rather than raising "
          f"(raised {type(exc).__name__})", False)

for proc in _spawned:
    proc.kill()

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all agent-ownership checks passed")
