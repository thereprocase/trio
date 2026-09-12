"""A displaced monitor must stop, and must say honestly WHY it stopped.

Revoking the loser's session token (see test-agent-reclaim.py) stops it from
SPEAKING, but the monitor holds no session — it reads the DB directly, keyed on
the members row. So without this check the displaced half of a duplicate
identity keeps waking on every mention, burning a billed turn per wake to
discover it cannot answer.

Revoked is NOT a synonym for displaced, which is why the event carries a
`reason`. _reap_sessions revokes anything idle a week; archive revokes every
session an agent has; an agent's own reconnect revokes its previous token. Two
of those want opposite advice — an archived agent told to "reconnect" would
partially UNDO its archive, because nth_connect has no archived_at gate and a
reclaim re-inserts the inbox membership and flips active back to 1.

The check is opt-in via --session-token. Absent one the monitor cannot tell
which session is its own, and any guess would eventually kill a healthy agent
that merely reconnected mid-run — so no token means no opinion, which is also
what keeps old launch commands working.

Usage: python tests/test-monitor-session-revoked.py
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
import nth_monitor as mon  # noqa: E402
import nth_server as srv  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="nth-mon-revoke-"))
srv.DB_DIR = tmp
srv.DB_PATH = tmp / "nth.db"

srv.nth_connect(summary="host", name="Host", channel="room")


def register_agent(agent_id, name, secret):
    db = sqlite3.connect(str(srv.DB_PATH))
    db.execute("INSERT INTO agents (id, name, model, state, managed, "
               "created_at, reclaim_secret) VALUES (?,?,'sonnet','spawning',1,?,?)",
               (agent_id, name, srv.now_iso(), secret))
    db.commit()
    db.close()


register_agent("ag_ayla", "Ayla", "SECRET")

first = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                   resume_member_id="ag_ayla",
                                   reclaim_secret="SECRET"))
first_token = first["session_token"]


def run_monitor(token, channel="room", member="ag_ayla", expect_exit=True,
                timeout=20.0):
    """Run the monitor in a thread and return (events, still_running).

    Two things Treebeard flagged, both fixed here:

      * Timing. The old version slept a fixed wall-clock window and inferred
        liveness from elapsed time. This polls for the thread to finish instead,
        so the exit path returns as fast as it can (the displacement check runs
        on the first loop iteration, before any sleep) and the generous ceiling
        only matters on a loaded machine. For the "keeps running" cases we wait
        a bounded moment and assert it is STILL alive.
      * Leaked threads. A surviving monitor kept polling the shared DB and could
        emit into a LATER case's captured list, because `mon.emit` is a module
        global that every call reassigns. Each case now gets its own emit
        closure that stops recording once the case is over, so a leaked thread
        cannot contaminate its successor.
    """
    events = []
    done = threading.Event()
    closed = threading.Event()

    def capture(payload):
        if not closed.is_set():
            events.append(payload)

    original_emit = mon.emit
    mon.emit = capture
    mon.DB_PATH = srv.DB_PATH

    def target():
        try:
            mon.monitor(channel, member, filter_mode="all",
                        _db_path=srv.DB_PATH, session_token=token)
        except SystemExit:
            pass
        except Exception as e:
            capture({"event": "crash", "msg": f"{type(e).__name__}: {e}"})
        finally:
            done.set()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    if expect_exit:
        done.wait(timeout=timeout)
    else:
        # Nothing to wait FOR — give it long enough to have ticked at least
        # once (the check runs pre-sleep on iteration one) and confirm it is
        # still going.
        done.wait(timeout=2.0)
    alive = t.is_alive()
    closed.set()
    mon.emit = original_emit
    return events, alive


def reason_of(events):
    for e in events:
        if e.get("event") == "session_revoked":
            return e.get("reason")
    return None


# ── the displaced monitor exits, cleanly ────────────────────────────────────
second = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                    resume_member_id="ag_ayla",
                                    reclaim_secret="SECRET"))
check("the twin's reclaim produced a new token",
      second["session_token"] != first_token)

events, still_running = run_monitor(first_token)
check("a monitor holding a revoked token exits", not still_running)
# Treebeard: "exits" alone passed pre-fix for the wrong reason — the thread died
# of a TypeError from the missing kwarg and the crash handler swallowed it. An
# exit is only correct if it was a deliberate return, so say so.
check("...cleanly — a returned event, not a crash",
      not any(e.get("event") == "crash" for e in events))
check("...and says why, in one terminal event",
      any(e.get("event") == "session_revoked" for e in events))
check("...naming the member and channel",
      any(e.get("event") == "session_revoked"
          and e.get("member_id") == "ag_ayla" and e.get("channel") == "room"
          for e in events))
check("...and does not leak the token into the event",
      first_token not in json.dumps(events))
check("...classified as a displacement, since a live successor exists",
      reason_of(events) == "displaced")


# ── the surviving monitor keeps running ─────────────────────────────────────
events, still_running = run_monitor(second["session_token"], expect_exit=False)
check("the CURRENT session's monitor keeps running", still_running)
check("...and does not report itself revoked",
      not any(e.get("event") == "session_revoked" for e in events))


# ── opt-in: no token, no opinion ────────────────────────────────────────────
events, still_running = run_monitor("", expect_exit=False)
check("a monitor launched WITHOUT a token is unaffected by revocation",
      still_running
      and not any(e.get("event") == "session_revoked" for e in events))


# ── an unknown token is treated as revoked ──────────────────────────────────
events, still_running = run_monitor("s_never_existed")
check("a token absent from the table also stops the monitor",
      not still_running and reason_of(events) is not None)


# ── a REAPED-AWAY row, not merely a never-existed one ───────────────────────
# _reap_sessions DELETEs rows a week after revocation, so "absent" has two
# histories. This one existed, was revoked, and then vanished — and with no
# successor the honest answer is "reconnect", not "you were displaced".
register_agent("ag_reap", "Reaper", "SECRET")
reaped = json.loads(srv.nth_connect(summary="r", name="Reaper", channel="room",
                                    resume_member_id="ag_reap",
                                    reclaim_secret="SECRET"))
db = sqlite3.connect(str(srv.DB_PATH))
db.execute("DELETE FROM sessions WHERE session_token = ?",
           (reaped["session_token"],))
db.commit()
db.close()
events, still_running = run_monitor(reaped["session_token"], member="ag_reap")
check("a reaped-away session stops the monitor", not still_running)
check("...and is NOT called a displacement — nothing replaced it",
      reason_of(events) == "invalidated")


# ── an ARCHIVED agent must not be told to reconnect ─────────────────────────
# Archive revokes every session and deactivates the member, leaving no
# successor — so the neutral "reconnect" branch would catch it. But nth_connect
# has no archived_at gate: a reclaim re-inserts the inbox membership and flips
# active back to 1, partially undoing the archive. Archive is checked first.
register_agent("ag_arch", "Archie", "SECRET")
archived = json.loads(srv.nth_connect(summary="x", name="Archie", channel="room",
                                      resume_member_id="ag_arch",
                                      reclaim_secret="SECRET"))
db = sqlite3.connect(str(srv.DB_PATH))
db.execute("UPDATE agents SET archived_at = ? WHERE id = 'ag_arch'",
           (srv.now_iso(),))
db.execute("UPDATE sessions SET revoked_at = ? WHERE member_id = 'ag_arch' "
           "AND revoked_at IS NULL", (srv.now_iso(),))
db.commit()
db.close()
events, still_running = run_monitor(archived["session_token"], member="ag_arch")
check("an archived agent's monitor stops", not still_running)
check("...classified as archived, not displaced or invalidated",
      reason_of(events) == "archived")
archived_msg = next((e.get("msg", "") for e in events
                     if e.get("event") == "session_revoked"), "")
check("...and is told NOT to reconnect",
      "not reconnect" in archived_msg.lower()
      or "do not reconnect" in archived_msg.lower())


# ── a FAILED reclaim must not revoke anything ───────────────────────────────
# The negative case: authentication is what gates the whole displacement sweep,
# so a wrong secret must leave the incumbent's capability untouched.
register_agent("ag_keep", "Keeper", "SECRET")
keeper = json.loads(srv.nth_connect(summary="k", name="Keeper", channel="room",
                                    resume_member_id="ag_keep",
                                    reclaim_secret="SECRET"))
refused = json.loads(srv.nth_connect(summary="k", name="Keeper", channel="room",
                                     resume_member_id="ag_keep",
                                     reclaim_secret="WRONG"))
check("a wrong-secret reclaim is refused", "error" in refused)
still_ok = json.loads(srv.nth_send("room", "ag_keep", "untouched",
                                   session_token=keeper["session_token"]))
check("...and the incumbent's session survives it", "error" not in still_ok)
events, still_running = run_monitor(keeper["session_token"], member="ag_keep",
                                    expect_exit=False)
check("...and its monitor keeps running", still_running)


# ── read_only tokens are outside the sweep ──────────────────────────────────
# The revoke is scoped to role='primary'. A read_only token carries no authority
# to duplicate, so a reclaim must leave it alone.
db = sqlite3.connect(str(srv.DB_PATH))
db.execute("INSERT INTO sessions (session_token, member_id, channel, role, "
           "connected_at, last_seen, last_read) "
           "VALUES ('s_readonly_probe','ag_ayla','room','read_only',?,?,0)",
           (srv.now_iso(), srv.now_iso()))
db.commit()
db.close()
srv.nth_connect(summary="a", name="Ayla", channel="room",
                resume_member_id="ag_ayla", reclaim_secret="SECRET")
db = sqlite3.connect(str(srv.DB_PATH))
ro_revoked = db.execute(
    "SELECT revoked_at FROM sessions WHERE session_token = 's_readonly_probe'"
).fetchone()[0]
db.close()
check("a reclaim does not revoke read_only sessions", ro_revoked is None)


# ── the new-channel branch revokes nothing ──────────────────────────────────
# Reclaiming into a channel that does not exist yet has no incumbent. Before
# the gate was widened this was covered by reclaimed_existing; it is asserted
# directly now so the widening cannot regress it silently.
fresh = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="brandnew",
                                   resume_member_id="ag_ayla",
                                   reclaim_secret="SECRET"))
check("a reclaim into a brand-new channel still yields a usable session",
      "error" not in json.loads(srv.nth_send(
          "brandnew", "ag_ayla", "hello", session_token=fresh["session_token"])))


# ── pre-sessions schema: no opinion, no crash ───────────────────────────────
legacy = Path(tempfile.mkdtemp(prefix="nth-mon-legacy-")) / "nth.db"
srv_db = sqlite3.connect(str(srv.DB_PATH))
srv_db.backup(sqlite3.connect(str(legacy)))
srv_db.close()
ldb = sqlite3.connect(str(legacy))
ldb.execute("DROP TABLE sessions")
ldb.commit()
ldb.close()
saved_path = srv.DB_PATH
events = []
_orig = mon.emit
mon.emit = lambda p: events.append(p)
mon.DB_PATH = legacy
t = threading.Thread(
    target=lambda: mon.monitor("room", "ag_ayla", filter_mode="all",
                               _db_path=legacy, session_token="s_whatever"),
    daemon=True)
t.start()
time.sleep(2.0)
legacy_alive = t.is_alive()
mon.emit = _orig
mon.DB_PATH = saved_path
check("a database with no sessions table does not stop the monitor",
      legacy_alive)
check("...and does not claim a revocation",
      not any(e.get("event") == "session_revoked" for e in events))


# ── the CLI flag parses ─────────────────────────────────────────────────────
check("--session-token is read from argv",
      mon.parse_session_token_arg(
          ["--filter", "about", "--session-token", "s_abc"]) == "s_abc")
check("...and is absent-safe",
      mon.parse_session_token_arg(["--filter", "about"]) == "")
check("...and does not disturb --filter parsing",
      mon.parse_filter_arg(["--session-token", "s_abc", "--filter", "at"])
      == "at")
try:
    mon.parse_session_token_arg(["--session-token"])
    check("a valueless --session-token is rejected", False)
except ValueError:
    check("a valueless --session-token is rejected", True)

# The hint the server hands back must actually carry the flag, or nothing
# above ever runs in production.
hint = first.get("monitor_hint", "")
check("connect's monitor_hint passes the session token",
      "--session-token" in hint and first_token in hint)

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")
