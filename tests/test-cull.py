"""Tests for removing a channel participant from the web dashboard (/api/cull).

Live loopback round-trip: an operator removes a member; their claimed task is
released back to open, a [culled] message is posted, and self / unknown targets
are rejected. Drives the real nth_web server + nth_server DB logic.

Usage: python tests/test-cull.py
"""
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
import shutil
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []
skips = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def skip(name, why):
    print(f"SKIP: {name} ({why})")
    skips.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_cull_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def connect(name, channel=""):
    r = json.loads(srv.nth_connect(summary="t", name=name, channel=channel))
    return r["channel"], r["member_id"]


def http(port, path, method="GET", body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


# ── unit: cull_member helper against an in-memory-ish real DB ────────────────
CH, asker = connect("Asker", channel="culltest")
_ch, victim = connect("Victim", channel=CH)
# Give the victim a claimed task so we can prove it gets released.
t = json.loads(srv.nth_send(channel=CH, member_id=asker, message="do a thing", task=True))
task_id = t["task_id"]
json.loads(srv.nth_claim(channel=CH, member_id=victim, task_id=task_id))

db = srv.get_db()
try:
    res, err = web.cull_member(db, CH, asker, "Asker", victim)
    db.commit()
    check("cull_member: ok", err is None and res and res["culled_id"] == victim)
    check("cull_member: released the claimed task", res and res["released_tasks"] == [task_id])
    gone = db.execute("SELECT 1 FROM members WHERE id=? AND channel=?", (victim, CH)).fetchone()
    check("cull_member: member row deleted", gone is None)
    live_sess = db.execute(
        "SELECT 1 FROM sessions WHERE channel=? AND member_id=? AND revoked_at IS NULL",
        (CH, victim)).fetchone()
    check("cull_member: victim's sessions revoked", live_sess is None)
    tstatus = db.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (task_id,)).fetchone()
    check("cull_member: task back to open", tstatus["status"] == "open" and tstatus["claimed_by"] is None)
    cmsg = db.execute(
        "SELECT member_id, member_name FROM messages WHERE channel=? AND content LIKE '[culled]%'",
        (CH,)).fetchone()
    check("cull_member: posts [culled] system message", cmsg is not None)
    check("cull_member: [culled] authored by the caller, not the victim",
          cmsg["member_id"] == asker and cmsg["member_name"] == "Asker")
    # self / unknown guards
    _r, e_self = web.cull_member(db, CH, asker, "Asker", asker)
    check("cull_member: self rejected", e_self is not None and "yourself" in e_self.lower())
    _r, e_unk = web.cull_member(db, CH, asker, "Asker", "nope")
    check("cull_member: unknown rejected", e_unk is not None and "not found" in e_unk.lower())
    # double-cull: the now-removed victim can't be culled again
    _r, e_again = web.cull_member(db, CH, asker, "Asker", victim)
    check("cull_member: double-cull -> not found", e_again is not None and "not found" in e_again.lower())
finally:
    db.close()


# ── unit: locks, channel isolation, task edge cases ──────────────────────────
CHL, askerL = connect("AskerL", channel="culltest3")
_ch, victimL = connect("VictimL", channel=CHL)
_ch, otherL = connect("OtherL", channel=CHL)
json.loads(srv.nth_lock(channel=CHL, member_id=victimL, resource="res-victim"))
json.loads(srv.nth_lock(channel=CHL, member_id=otherL, resource="res-other"))
# victim posts an OPEN task they never claim — must NOT be released/altered.
tp = json.loads(srv.nth_send(channel=CHL, member_id=victimL, message="unclaimed", task=True))
open_task = tp["task_id"]
# victim claims two other tasks — both must be released.
t1 = json.loads(srv.nth_send(channel=CHL, member_id=askerL, message="t1", task=True))["task_id"]
t2 = json.loads(srv.nth_send(channel=CHL, member_id=askerL, message="t2", task=True))["task_id"]
json.loads(srv.nth_claim(channel=CHL, member_id=victimL, task_id=t1))
json.loads(srv.nth_claim(channel=CHL, member_id=victimL, task_id=t2))

db = srv.get_db()
try:
    res, err = web.cull_member(db, CHL, askerL, "AskerL", victimL)
    db.commit()
    check("cull: releases multiple claimed tasks", err is None and set(res["released_tasks"]) == {t1, t2})
    for tid in (t1, t2):
        r = db.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (tid,)).fetchone()
        check(f"cull: task #{tid} back to open", r["status"] == "open" and r["claimed_by"] is None)
    vlock = db.execute("SELECT 1 FROM locks WHERE channel=? AND held_by=?", (CHL, victimL)).fetchone()
    check("cull: victim's lock released", vlock is None)
    olock = db.execute("SELECT 1 FROM locks WHERE channel=? AND held_by=?", (CHL, otherL)).fetchone()
    check("cull: other member's lock survives", olock is not None)
    ot = db.execute("SELECT status, posted_by, claimed_by FROM tasks WHERE id=?", (open_task,)).fetchone()
    check("cull: victim's own unclaimed task untouched",
          ot["status"] == "open" and ot["posted_by"] == victimL and ot["claimed_by"] is None)
    # channel isolation: can't cull a member of a different channel
    _r, e_x = web.cull_member(db, "culltest2", askerL, "AskerL", otherL)
    check("cull: cross-channel target rejected", e_x is not None and "not found" in e_x.lower())
finally:
    db.close()


# ── authz policy: guests may not cull (Aragorn) ──────────────────────────────
# NOTE: these four assert POLICY only (what is in the tuple). The behavioural
# counterpart — that the endpoint actually refuses — lives in the live-server
# section below. Without it the whole 403 gate can be deleted and this file
# stays green, which is exactly what a review found.
check("authz: guest not allowed to cull", web.IDENTITY_SOURCE_GUEST not in web.CULL_ALLOWED_SOURCES)
check("authz: pending not allowed to cull", web.IDENTITY_SOURCE_PENDING not in web.CULL_ALLOWED_SOURCES)
check("authz: loopback allowed", web.IDENTITY_SOURCE_LOOPBACK in web.CULL_ALLOWED_SOURCES)
check("authz: tailscale allowed", web.IDENTITY_SOURCE_TAILSCALE in web.CULL_ALLOWED_SOURCES)


# ── live: /api/cull round-trip ───────────────────────────────────────────────
CH2, asker2 = connect("LiveAsker", channel="culltest2")
_ch, victim2 = connect("LiveVictim", channel=CH2)
hub = web.EventHub(srv.DB_PATH, CH2)
server = None
try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH2
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    # First send creates the loopback operator (id prefixed _op_).
    st, _ = http(port, "/api/send", "POST", {"content": "hi"})
    if st != 200:
        skip("live cull", f"loopback send not accepted ({st})")
    else:
        db = srv.get_db()
        try:
            # Both underscores escaped: '_' is a LIKE single-char wildcard, so
            # an unescaped leading one would match any <c>op_... id. Nothing mints
            # such an id today, but that is an implicit invariant, not a guarantee.
            op = db.execute("SELECT id FROM members WHERE channel=? "
                            "AND id LIKE '\\_op\\_%' ESCAPE '\\'",
                            (CH2,)).fetchone()
        finally:
            db.close()
        op_id = op["id"]

        # missing target
        st, _ = http(port, "/api/cull", "POST", {})
        check("live: missing target -> 400", st == 400)
        # unknown target
        st, _ = http(port, "/api/cull", "POST", {"target_member_id": "ghost"})
        check("live: unknown target -> 400", st == 400)
        # self
        st, _ = http(port, "/api/cull", "POST", {"target_member_id": op_id})
        check("live: self-cull -> 400", st == 400)
        # real removal
        st, resp = http(port, "/api/cull", "POST", {"target_member_id": victim2})
        check("live: cull accepted", st == 200 and resp.get("culled_id") == victim2)
        db = srv.get_db()
        try:
            gone = db.execute("SELECT 1 FROM members WHERE id=? AND channel=?",
                              (victim2, CH2)).fetchone()
        finally:
            db.close()
        check("live: member removed from roster", gone is None)

        # crash guards: non-string target + non-dict body → clean 400 (not 500/drop)
        st, _ = http(port, "/api/cull", "POST", {"target_member_id": 123})
        check("live: non-string target -> 400", st == 400)
        st, _ = http(port, "/api/cull", "POST", [1, 2, 3])
        check("live: non-dict body -> 400", st == 400)
        # double-cull the already-removed victim2 → 400
        st, _ = http(port, "/api/cull", "POST", {"target_member_id": victim2})
        check("live: double-cull -> 400", st == 400)
        # cross-channel: `asker` belongs to CH (culltest), not this channel (CH2)
        st, _ = http(port, "/api/cull", "POST", {"target_member_id": asker})
        check("live: cross-channel target -> 400", st == 400)
        # full endpoint task-release path: victim with a claimed task, culled live
        _c, victim2b = connect("LiveVictim2", channel=CH2)
        tb = json.loads(srv.nth_send(channel=CH2, member_id=asker2, message="tb", task=True))["task_id"]
        json.loads(srv.nth_claim(channel=CH2, member_id=victim2b, task_id=tb))
        st, resp = http(port, "/api/cull", "POST", {"target_member_id": victim2b})
        check("live: task-release via endpoint", st == 200 and resp.get("released_tasks") == [tb])
        db = srv.get_db()
        try:
            rr = db.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (tb,)).fetchone()
        finally:
            db.close()
        check("live: released task is open in DB", rr["status"] == "open" and rr["claimed_by"] is None)

        # Landing mode serves many channels from one process. It intentionally
        # leaves NthWebHandler.channel empty, so this catches endpoints that
        # accidentally use process-wide state instead of ?channel=.
        saved_channel = web.NthWebHandler.channel
        web.NthWebHandler.landing_mode = True
        web.NthWebHandler.channel = ""
        try:
            _c, landing_victim = connect("LandingVictim", channel=CH2)
            st, resp = http(port, f"/api/cull?channel={CH2}", "POST",
                            {"target_member_id": landing_victim})
            check("landing: cull resolves channel from ?channel=",
                  st == 200 and resp.get("culled_id") == landing_victim)
            st, _ = http(port, "/api/cull", "POST",
                         {"target_member_id": landing_victim})
            check("landing: cull without channel -> 400", st == 400)
        finally:
            web.NthWebHandler.landing_mode = False
            web.NthWebHandler.channel = saved_channel
except OSError as e:
    skip("live cull", f"could not start server: {e}")

    # ── behavioural authz: the endpoint must actually refuse a guest ─────────
    # The tuple-membership checks above cannot catch the 403 gate being deleted.
    # Force a guest identity and assert the HTTP response, not the policy data.
    try:
        _real = web.NthWebHandler._resolve_identity
        class _Guest:
            source = web.IDENTITY_SOURCE_GUEST
            name = "guest"
            summary = "guest"
        web.NthWebHandler._resolve_identity = lambda self: (None, _Guest(), False)
        st_g, body_g = http(port, "/api/cull", "POST", {"target_member_id": victim2})
        check("live authz: a guest identity is refused with 403", st_g == 403)
        check("live authz: refusal explains the trust requirement",
              "trusted" in json.dumps(body_g).lower())
        db2 = srv.get_db()
        try:
            still = db2.execute("SELECT 1 FROM members WHERE channel=? AND id=?",
                                (CH2, victim2)).fetchone()
        finally:
            db2.close()
        check("live authz: refused cull did not remove the member", still is not None)
    finally:
        web.NthWebHandler._resolve_identity = _real

    # ── landing mode: cull must resolve the channel from the request ─────────
    # Landing mode serves many channels from one process and never sets
    # NthWebHandler.channel; binding it would make cull silently dead there.
    try:
        web.NthWebHandler.landing_mode = True
        _saved_ch = web.NthWebHandler.channel
        web.NthWebHandler.channel = ""
        _ch3, victim3 = connect("LandingVictim", channel=CH2)
        st_l, _ = http(port, f"/api/cull?channel={CH2}", "POST",
                       {"target_member_id": victim3})
        check("landing: cull resolves the channel from ?channel=", st_l == 200)
        st_n, _ = http(port, "/api/cull", "POST", {"target_member_id": victim3})
        check("landing: cull without a channel is a clean 400", st_n == 400)
    finally:
        web.NthWebHandler.landing_mode = False
        web.NthWebHandler.channel = _saved_ch

finally:
    if server is not None:
        server.shutdown()
    hub.stop()


shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), {len(skips)} skip(s)")
sys.exit(1 if failures else 0)
