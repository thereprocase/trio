"""Tests for the agent control-plane endpoints (supervisor-backed):
POST /api/agents (create+spawn), GET /api/agents (roster),
POST /api/agents/<id>/{stop,archive,unarchive}. Operator-only. Driven against the fake
stream-json agent (tests/fake_agent.py) — NO real billed Claude session.

Usage: python tests/test-web-agents.py
"""
import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ["TRIO_AGENT_CMD"] = f"{sys.executable} {HERE / 'fake_agent.py'}"

import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_agents_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def http(port, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def row(agent_id):
    db = sqlite3.connect(str(srv.DB_PATH)); db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    finally:
        db.close()


json.loads(srv.nth_connect(summary="t", name="Host", channel="chan-x"))

web.NthWebHandler._default_channel = ""
web.NthWebHandler.db_path = srv.DB_PATH
web._DB_PATH_GLOBAL = srv.DB_PATH
web._SUPERVISOR = None  # fresh supervisor bound to the temp DB

server = None
foreign_archive_proc = None
try:
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    st, health = http(port, "/api/health")
    check("health: database and configured agent runtime are ready",
          st == 200 and health.get("ready") is True
          and health.get("runtime", {}).get("provider") == "claude"
          and health.get("database", {}).get("quick_check") == "ok")

    # ── create + spawn ──
    st, d = http(port, "/api/agents", "POST",
                 {"model": "sonnet", "channels": ["chan-x"], "prompt": "be helpful"})
    agent = d.get("agent", {})
    aid = agent.get("id", "")
    check("create: 200 + live", st == 200 and agent.get("live"))
    check("create: auto themed name assigned", bool(agent.get("name")))
    check("create: placed in chan-x", agent.get("channels") == ["chan-x"])
    # The supervisor's spawn() flips state to "running" only after the process
    # proves alive, and the startup nudge can complete before this read moves
    # it to "idle". Assert the durable invariant (live process + active state),
    # not which side of that legitimate transition the test catches.
    r = row(aid)
    deadline = time.monotonic() + 1.0
    while (not r or r["state"] not in ("running", "idle")
           or not web.get_supervisor().is_running(aid)) and time.monotonic() < deadline:
        time.sleep(0.05)
        r = row(aid)
    check("create: agents row active",
          r and r["state"] in ("running", "idle")
          and web.get_supervisor().is_running(aid))
    check("create: session_id captured", r and r["session_id"] == "sess-fake-sonnet-001")
    db = sqlite3.connect(str(srv.DB_PATH))
    ac = db.execute("SELECT COUNT(*) FROM agent_channels WHERE agent_id=?", (aid,)).fetchone()[0]
    mem = db.execute("SELECT kind FROM members WHERE id=? AND channel='chan-x'", (aid,)).fetchone()
    db.close()
    check("create: public placement + private DM inbox rows", ac == 2)
    check("create: members row is kind=agent", mem and mem[0] == "agent")

    # ── roster ──
    st, d = http(port, "/api/agents")
    ids = {a["id"]: a for a in d.get("agents", [])}
    check("list: 200 + includes agent, live, channels", st == 200
          and aid in ids and ids[aid]["live"] and ids[aid]["channels"] == ["chan-x"]
          and ids[aid]["abandoned"] is False)

    # ── bogus channel rejected ──
    st, _ = http(port, "/api/agents", "POST", {"model": "sonnet", "channels": ["ghost"]})
    check("create with unknown channel -> 400", st == 400)

    # ── stop ──
    st, _ = http(port, f"/api/agents/{aid}/stop", "POST")
    time.sleep(0.2)
    check("stop: 200 + row stopped + not live", st == 200
          and row(aid)["state"] == "stopped"
          and not web.get_supervisor().is_running(aid))

    # ── archive (soft-delete: row + placements kept, presence + sessions revoked) ──
    db = sqlite3.connect(str(srv.DB_PATH))
    db.execute("INSERT INTO sessions (session_token,member_id,channel,role,fingerprint,connected_at,last_seen) "
               "VALUES ('delete-token',?,'chan-x','primary','delete-test',?,?)",
               (aid, srv.now_iso(), srv.now_iso()))
    db.commit(); db.close()
    st, _ = http(port, f"/api/agents/{aid}/archive", "POST")
    ar = row(aid)
    check("archive: 200 + agents row kept + archived_at stamped",
          st == 200 and ar is not None and ar["archived_at"] is not None)
    db = sqlite3.connect(str(srv.DB_PATH))
    left = db.execute("SELECT COUNT(*) FROM agent_channels WHERE agent_id=?", (aid,)).fetchone()[0]
    public_placement = db.execute(
        "SELECT COUNT(*) FROM agent_channels WHERE agent_id=? AND channel='chan-x'", (aid,)
    ).fetchone()[0]
    inbox_member = db.execute(
        "SELECT 1 FROM members WHERE id=? AND channel=?", (aid, srv.AGENT_INBOX_CHANNEL)
    ).fetchone()
    active = db.execute("SELECT active FROM members WHERE id=? AND channel='chan-x'", (aid,)).fetchone()
    revoked = db.execute("SELECT revoked_at FROM sessions WHERE session_token='delete-token'").fetchone()
    db.close()
    check("archive: public placement retained, inbox presence removed",
          left == 1 and public_placement == 1 and inbox_member is None)
    check("archive: public member deactivated", active and active[0] == 0)
    check("archive: outstanding MCP sessions revoked", revoked and bool(revoked[0]))
    # archived agents are excluded from the default roster
    st, d = http(port, "/api/agents", "GET")
    ids = [a["id"] for a in d.get("agents", [])]
    check("archive: hidden from default roster", aid not in ids)
    st, d = http(port, "/api/agents?archived=1", "GET")
    ids = [a["id"] for a in d.get("agents", [])]
    check("archive: surfaced under ?archived=1", aid in ids)

    # Archive is destructive too: it must pass through the same cross-hub
    # ownership guard as stop/hibernate/clear. The pid is the only durable
    # evidence that another hub owns this live process, so stamping pid=NULL
    # before trying to stop it makes the guard blind and permits a duplicate
    # agent on the next wake.
    foreign_id = "ag_foreign_archive"
    marker = web.nsup.AGENT_ID_MARKER.format(agent_id=foreign_id)
    foreign_archive_proc = subprocess.Popen(
        [sys.executable, "-c", f"import time # {marker}\ntime.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if web.nsup._pid_cmdline(foreign_archive_proc.pid):
            break
        time.sleep(0.05)
    foreign_now = srv.now_iso()
    db = sqlite3.connect(str(srv.DB_PATH))
    db.execute(
        "INSERT INTO agents (id,name,model,state,managed,pid,created_at,last_active_at) "
        "VALUES (?,?,'sonnet','running',1,?,?,?)",
        (foreign_id, "ForeignArchive", foreign_archive_proc.pid,
         foreign_now, foreign_now))
    db.execute(
        "INSERT INTO members (id,channel,name,last_seen,joined_at,kind,active) "
        "VALUES (?, 'chan-x', ?, ?, ?, 'agent', 1)",
        (foreign_id, "ForeignArchive", foreign_now, foreign_now))
    db.execute("INSERT INTO agent_channels (agent_id,channel,member_id,joined_at) "
               "VALUES (?, 'chan-x', ?, ?)",
               (foreign_id, foreign_id, foreign_now))
    db.execute(
        "INSERT INTO sessions "
        "(session_token,member_id,channel,role,fingerprint,connected_at,last_seen) "
        "VALUES ('foreign-archive-token',?,'chan-x','primary','foreign',?,?)",
        (foreign_id, foreign_now, foreign_now))
    db.commit()
    db.close()

    st, detail = http(port, f"/api/agents/{foreign_id}/archive", "POST")
    foreign_row = row(foreign_id)
    db = sqlite3.connect(str(srv.DB_PATH))
    foreign_active = db.execute(
        "SELECT active FROM members WHERE id=? AND channel='chan-x'",
        (foreign_id,)).fetchone()
    foreign_revoked = db.execute(
        "SELECT revoked_at FROM sessions WHERE session_token='foreign-archive-token'"
    ).fetchone()
    db.close()
    check("archive: refuses a live process owned by another hub",
          st == 409 and str(foreign_archive_proc.pid) in detail.get("error", ""))
    check("archive: preserves foreign ownership evidence and durable state",
          foreign_row is not None
          and foreign_row["pid"] == foreign_archive_proc.pid
          and foreign_row["state"] == "running"
          and foreign_row["archived_at"] is None)
    check("archive: refusal leaves presence and sessions untouched",
          foreign_active == (1,) and foreign_revoked == (None,))

    # ── unarchive (restore presence; agent stays stopped until woken) ──
    # While this identity is archived its portrait is reusable. If another
    # active identity takes it, unarchive must atomically choose a different
    # free portrait rather than reintroducing duplicate faces.
    archived_avatar = ar["avatar_name"]
    db = sqlite3.connect(str(srv.DB_PATH))
    db.execute(
        "INSERT INTO agents (id,name,model,state,managed,avatar_name,created_at) "
        "VALUES ('avatar-reuser','AvatarReuser','external','stopped',0,?,?)",
        (archived_avatar, srv.now_iso()))
    db.commit(); db.close()
    st, _ = http(port, f"/api/agents/{aid}/unarchive", "POST")
    ur = row(aid)
    check("unarchive: 200 + archived_at cleared", st == 200 and ur and ur["archived_at"] is None)
    check("unarchive: resolves a portrait reused while archived",
          ur and ur["avatar_name"] and ur["avatar_name"] != archived_avatar)
    db = sqlite3.connect(str(srv.DB_PATH))
    active = db.execute("SELECT active FROM members WHERE id=? AND channel='chan-x'", (aid,)).fetchone()
    inbox_member = db.execute(
        "SELECT active FROM members WHERE id=? AND channel=?", (aid, srv.AGENT_INBOX_CHANNEL)
    ).fetchone()
    inbox_placement = db.execute(
        "SELECT 1 FROM agent_channels WHERE agent_id=? AND channel=?",
        (aid, srv.AGENT_INBOX_CHANNEL),
    ).fetchone()
    db.execute("DELETE FROM agents WHERE id='avatar-reuser'")
    db.commit()
    db.close()
    check("unarchive: member presence restored", active and active[0] == 1)
    check("unarchive: global inbox presence restored",
          inbox_member and inbox_member[0] == 1 and inbox_placement is not None)
    st, d = http(port, "/api/agents", "GET")
    ids = [a["id"] for a in d.get("agents", [])]
    check("unarchive: visible in default roster again", aid in ids)

    # ── archived agents are frozen: lifecycle actions rejected (W6-W8) ──
    st, _ = http(port, f"/api/agents/{aid}/archive", "POST")  # re-archive first
    st, _ = http(port, f"/api/agents/{aid}/wake", "POST")
    check("archived agent: wake rejected with 409", st == 409)
    st, _ = http(port, f"/api/agents/{aid}/clear", "POST")
    check("archived agent: clear rejected with 409", st == 409)
    st, _ = http(port, f"/api/agents/{aid}/stop", "POST")
    check("archived agent: stop rejected with 409", st == 409)
    http(port, f"/api/agents/{aid}/unarchive", "POST")  # clean up

    # ── unarchive does NOT re-add a removed channel (C2) ──
    # Create a fresh agent in chan-x, remove it from chan-x, then archive +
    # unarchive. The agent should NOT be re-placed in chan-x.
    st, d = http(port, "/api/agents", "POST", {"model": "sonnet", "channels": ["chan-x"]})
    rid = d.get("agent", {}).get("id")
    http(port, f"/api/agents/{rid}/placement", "POST", {"channel": "chan-x", "present": False})
    db = sqlite3.connect(str(srv.DB_PATH))
    ac_before = db.execute("SELECT COUNT(*) FROM agent_channels WHERE agent_id=? AND channel='chan-x'", (rid,)).fetchone()[0]
    db.close()
    check("C2: agent removed from chan-x before archive", ac_before == 0)
    http(port, f"/api/agents/{rid}/archive", "POST")
    http(port, f"/api/agents/{rid}/unarchive", "POST")
    db = sqlite3.connect(str(srv.DB_PATH))
    ac_after = db.execute("SELECT COUNT(*) FROM agent_channels WHERE agent_id=? AND channel='chan-x'", (rid,)).fetchone()[0]
    mem_active = db.execute("SELECT active FROM members WHERE id=? AND channel='chan-x'", (rid,)).fetchone()
    db.close()
    check("C2: unarchive does not re-add removed channel placement", ac_after == 0)
    check("C2: unarchive does not reactivate removed channel presence",
          mem_active is None or mem_active[0] == 0)
    http(port, f"/api/agents/{rid}/archive", "POST")  # clean up

    # ── thinking-level (effort) ──
    st, d = http(port, "/api/agents", "POST",
                 {"model": "haiku", "channels": ["chan-x"], "effort": "high"})
    eid = d.get("agent", {}).get("id")
    er = row(eid)
    check("create with effort=high: stored on row", er and er["effort"] == "high")
    proc = web.get_supervisor()._procs.get(eid)
    check("effort passed to the spawned argv (--effort high)",
          proc and "--effort" in proc.argv and proc.argv[proc.argv.index("--effort") + 1] == "high")
    http(port, f"/api/agents/{eid}/archive", "POST")
    st, _ = http(port, "/api/agents", "POST",
                 {"model": "haiku", "channels": ["chan-x"], "effort": "bogus"})
    check("create with invalid effort -> 400", st == 400)

    # ── input validation: channels must be a list (Uruk-Hai) ──
    st, _ = http(port, "/api/agents", "POST", {"model": "sonnet", "channels": "chan-x"})
    check("create with channels as a STRING -> 400 (not a crash)", st == 400)
    st, _ = http(port, "/api/agents", "POST", {"model": "sonnet", "channels": 123})
    check("create with channels as an INT -> 400 (not a 500)", st == 400)

    # ── create with NO public channels (still directly messageable) ──
    st, d = http(port, "/api/agents", "POST", {"model": "sonnet", "channels": []})
    ab = d.get("agent", {})
    check("create with no channels -> 200, empty channels", st == 200 and ab.get("channels") == [])
    st, d = http(port, "/api/agents")
    match = [a for a in d.get("agents", []) if a["id"] == ab.get("id")]
    check("zero-placement agent has a private inbox and is not abandoned",
          match and match[0]["dm_ready"] is True
          and match[0]["abandoned"] is False
          and match[0]["channels"] == [])
    # Addressing that inbox from the dashboard is the DM feature's surface
    # (/api/dms + recipient-scoped sends), which arrives separately. What
    # matters here is that the supervisor gives every agent an inbox to be
    # addressed ON, asserted above via dm_ready.
    http(port, f"/api/agents/{ab.get("id")}/archive", "POST")

    # ── wake endpoint ──
    st, d = http(port, "/api/agents", "POST", {"model": "sonnet", "channels": ["chan-x"]})
    wid = d["agent"]["id"]
    http(port, f"/api/agents/{wid}/stop", "POST")
    time.sleep(0.2)
    st, _ = http(port, f"/api/agents/{wid}/wake", "POST")
    time.sleep(0.3)
    check("wake endpoint -> 200 + agent live again",
          st == 200 and web.get_supervisor().is_running(wid))
    st, _ = http(port, f"/api/agents/{wid}/compact", "POST",
                 {"message": "Keep the current plan"})
    check("compact endpoint accepts guidance for a live agent", st == 200)
    old_proc = web.get_supervisor()._procs.get(wid)
    st, _ = http(port, f"/api/agents/{wid}/clear", "POST")
    new_proc = web.get_supervisor()._procs.get(wid)
    check("clear endpoint -> fresh live process without --resume",
          st == 200 and new_proc is not None and new_proc is not old_proc
          and "--resume" not in new_proc.argv)
    st, _ = http(port, f"/api/agents/{wid}/hibernate", "POST")
    check("hibernate endpoint -> sleeping + not live",
          st == 200 and row(wid)["state"] == "sleeping"
          and not web.get_supervisor().is_running(wid))

    # Placement add/remove. Create a second real channel first.
    json.loads(srv.nth_connect(summary="t", name="Host2", channel="chan-y"))
    st, _ = http(port, f"/api/agents/{wid}/placement", "POST",
                 {"channel": "chan-y", "present": True})
    db = sqlite3.connect(str(srv.DB_PATH))
    placed = db.execute("SELECT COUNT(*) FROM agent_channels WHERE agent_id=? AND channel='chan-y'",
                        (wid,)).fetchone()[0]
    db.close()
    check("placement endpoint adds channel membership", st == 200 and placed == 1)
    st, _ = http(port, f"/api/agents/{wid}/placement", "POST",
                 {"channel": "chan-y", "present": False})
    db = sqlite3.connect(str(srv.DB_PATH))
    placed = db.execute("SELECT COUNT(*) FROM agent_channels WHERE agent_id=? AND channel='chan-y'",
                        (wid,)).fetchone()[0]
    db.close()
    check("placement endpoint removes channel membership", st == 200 and placed == 0)
    st, _ = http(port, "/api/agents/nope/wake", "POST")
    check("wake bogus agent -> 404", st == 404)
    http(port, f"/api/agents/{wid}/archive", "POST")

    # ── runtime preflight fails before creating a broken durable row ──
    _health = web.runtime_health
    web.runtime_health = lambda refresh=False, **_kwargs: {
        "provider": "claude", "ready": False,
        "detail": "Claude Code is not authenticated; run `claude login`",
    }
    try:
        with sqlite3.connect(str(srv.DB_PATH)) as check_db:
            before = check_db.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        st, d = http(port, "/api/agents", "POST", {"model": "sonnet", "channels": []})
        with sqlite3.connect(str(srv.DB_PATH)) as check_db:
            after = check_db.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        check("create: unavailable runtime returns actionable 409 before DB insert",
              st == 409 and "claude login" in d.get("error", "") and before == after)
    finally:
        web.runtime_health = _health

    # Legacy single-channel dashboards are viewers, never a second supervisor.
    web.NthWebHandler._agent_control_enabled = False
    try:
        st, d = http(port, "/api/agents")
        check("single-channel viewer refuses managed-agent control",
              st == 409 and "disabled" in d.get("error", ""))
    finally:
        web.NthWebHandler._agent_control_enabled = True

    # ── operator-only ──
    # Demote this request's identity to an untrusted source. The agent control
    # plane gates on ident.source (loopback or tailnet), not on the member id,
    # so a guest is simulated by making the ladder resolve to one.
    _orig = web.NthWebHandler._resolve_identity

    def _guest(self):
        token, ident, is_new = _orig(self)
        return token, dataclasses.replace(ident, source="guest"), is_new

    web.NthWebHandler._resolve_identity = _guest
    try:
        st, _ = http(port, "/api/agents")
        check("guest: GET /api/agents -> 403", st == 403)
        st, _ = http(port, "/api/agents", "POST", {"model": "sonnet"})
        check("guest: POST /api/agents -> 403", st == 403)
        st, _ = http(port, f"/api/agents/{aid}/archive", "POST")
        check("guest: POST /api/agents/<id>/archive -> 403", st == 403)
        st, _ = http(port, f"/api/agents/{aid}/unarchive", "POST")
        check("guest: POST /api/agents/<id>/unarchive -> 403", st == 403)
        st, _ = http(port, "/api/health")
        check("guest: GET /api/health -> 403", st == 403)
    finally:
        web.NthWebHandler._resolve_identity = _orig
finally:
    if foreign_archive_proc is not None:
        try:
            foreign_archive_proc.kill()
            foreign_archive_proc.wait(timeout=2)
        except Exception:
            pass
    if server is not None:
        server.shutdown()
    if web._SUPERVISOR is not None:
        web._SUPERVISOR.shutdown()

shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
