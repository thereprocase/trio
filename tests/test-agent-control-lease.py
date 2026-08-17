"""Only one hub may drive the agents in a database.

Two nth_web instances against one database both ran the agent control plane —
router, idle reaper and startup resume — because the only thing stopping a
second one was `args.channel is None`, which is true for both landing-mode
hubs. They then spawned duplicate agents.

The lease is the policy half of the fix (the enforcement half is the per-agent
ownership check in nth_supervisor, covered by test-agent-ownership.py). What it
has to get right:

  * two hubs starting at the same instant produce exactly one winner,
  * a released lease is immediately available — no waiting out a TTL,
  * an expired lease is takeable, so a hub that was SIGKILLed doesn't lock the
    control plane forever,
  * a crashed same-host holder is takeable AT ONCE via its pid, because the
    pid is knowable and making a restart wait a full TTL is pure downtime,
  * a live holder is NOT takeable, which is the whole point, and
  * renewal fails once someone else has taken over, so the loser finds out.

Usage: python tests/test-agent-control-lease.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

_tmp = Path(tempfile.mkdtemp(prefix="nth_lease_"))
os.environ["NTH_HOME"] = str(_tmp)

try:
    import nth_web as nw                 # noqa: E402
except ImportError as exc:               # mcp SDK absent — run-all reports skip
    print(f"SKIP: nth_web import failed ({exc})")
    sys.exit(0)

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


DB = _tmp / "nth.db"
sqlite3.connect(str(DB)).close()          # the file must exist, nothing more


def lease(**kw):
    return nw.AgentControlLease(DB, **kw)


# ── a free lease is takeable, a held one is not ────────────────────────────
first = lease()
check("first hub acquires the lease", first.acquire() is None)

second = lease()
blocked = second.acquire()
check("second hub is refused while the first holds it", blocked is not None)
check("the refusal names the holder so the operator can find it",
      blocked and blocked.get("holder") == first.holder
      and blocked.get("pid") == first.pid)

check("the holder can re-acquire its own lease (restart idempotence)",
      first.acquire() is None)

# ── release makes it immediately available ─────────────────────────────────
first.release()
check("a released lease is takeable at once, without waiting out the TTL",
      second.acquire() is None)
second.release()

# ── expiry ─────────────────────────────────────────────────────────────────
short = lease(ttl=0.05)
check("short-ttl hub acquires", short.acquire() is None)
time.sleep(0.2)
taker = lease()
check("an expired lease is takeable (holder was SIGKILLed, never released)",
      taker.acquire() is None)
check("the expired holder's renewal now fails, so it learns it lost",
      short.renew() is False)
taker.release()

# ── a dead same-host holder is takeable at once, on pid, not on TTL ────────
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
db = sqlite3.connect(str(DB))
db.execute("CREATE TABLE IF NOT EXISTS agent_control_lease ("
           "id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL, "
           "host TEXT NOT NULL DEFAULT '', pid INTEGER, "
           "acquired_at TEXT NOT NULL, expires_at REAL NOT NULL)")
db.execute("INSERT OR REPLACE INTO agent_control_lease VALUES "
           "(1,?,?,?,?,?)",
           ("ghost", nw.socket.gethostname(), dead.pid, "now",
            time.time() + 9999))
db.commit()
db.close()

heir = lease()
check("a crashed same-host holder is taken over immediately on its dead pid",
      heir.acquire() is None)
heir.release()

# The same row, but held by a live process, must NOT be taken over — this is
# the case the pid shortcut could get catastrophically wrong.
alive = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
try:
    db = sqlite3.connect(str(DB))
    db.execute("INSERT OR REPLACE INTO agent_control_lease VALUES "
               "(1,?,?,?,?,?)",
               ("busy-hub", nw.socket.gethostname(), alive.pid, "now",
                time.time() + 9999))
    db.commit()
    db.close()
    check("a LIVE same-host holder is not taken over",
          lease().acquire() is not None)

    # A holder on another machine has an unknowable pid: only the TTL may
    # decide, never a local pid lookup that would collide by coincidence.
    db = sqlite3.connect(str(DB))
    db.execute("INSERT OR REPLACE INTO agent_control_lease VALUES "
               "(1,?,?,?,?,?)",
               ("remote-hub", "some-other-host", 999999, "now",
                time.time() + 9999))
    db.commit()
    db.close()
    check("an unexpired holder on another host is not taken over on a local pid",
          lease().acquire() is not None)
finally:
    alive.kill()

# ── the actual race: N hubs starting together ──────────────────────────────
sqlite3.connect(str(DB)).execute(
    "DELETE FROM agent_control_lease").connection.commit()

winners = []
lock = threading.Lock()
start = threading.Barrier(8)


def contend():
    lz = lease()
    start.wait()
    if lz.acquire() is None:
        with lock:
            winners.append(lz.holder)


threads = [threading.Thread(target=contend) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check(f"8 hubs starting simultaneously produce exactly 1 winner "
      f"(got {len(winners)})", len(winners) == 1)

# ── renewal actually extends the hold ─────────────────────────────────────
# Without this, `expires_at = time.time()` instead of `time.time() + ttl`
# passes every other check in this file while making renewal a no-op: a live,
# actively-renewing hub would still lose its lease once the ORIGINAL ttl ran
# out, and nothing here would notice.
sqlite3.connect(str(DB)).execute("DELETE FROM agent_control_lease"
                                 ).connection.commit()
renewing = lease(ttl=0.4)
check("renewing hub acquires", renewing.acquire() is None)
check("renew() reports success while we still hold it", renewing.renew() is True)
time.sleep(0.3)
renewing.renew()
time.sleep(0.3)                       # past the ORIGINAL ttl, inside renewed
check("a renewed lease is still held past its original expiry",
      lease().acquire() is not None)
renewing.release()

# ── the renewal thread runs, and stops ────────────────────────────────────
sqlite3.connect(str(DB)).execute("DELETE FROM agent_control_lease"
                                 ).connection.commit()
threaded = lease(ttl=0.4, renew_interval=0.1)
check("threaded hub acquires", threaded.acquire() is None)
threaded.start_renewal()
time.sleep(0.9)                       # >2 original TTLs
check("the renewal thread keeps the lease alive on its own",
      lease().acquire() is not None)
threaded.stop()
threaded._thread.join(timeout=2)
check("stop() ends the renewal thread", not threaded._thread.is_alive())
gone = sqlite3.connect(str(DB)).execute(
    "SELECT COUNT(*) FROM agent_control_lease").fetchone()[0]
check("stop() releases the row so the next hub need not wait", gone == 0)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all agent-control-lease checks passed")
