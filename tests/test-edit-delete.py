"""Tests for operator edit/delete of own messages (POST /api/edit, /api/delete).

Live loopback: the operator edits/deletes their own web message; an agent's
message and missing/foreign targets are rejected; edit re-parses @mentions and
delete marks retracted + posts a synthetic [retracted #N].
Usage: python tests/test-edit-delete.py
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


_tmp = tempfile.mkdtemp(prefix="nth_edit_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def http(port, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def row(mid):
    db = srv.get_db()
    try:
        return db.execute(
            "SELECT content, mentions, retracted_at, retraction_reason, edited_at "
            "FROM messages WHERE id=?", (mid,)).fetchone()
    finally:
        db.close()


r = json.loads(srv.nth_connect(summary="t", name="Asker", channel="edittest"))
CH, asker = r["channel"], r["member_id"]
# An agent-authored message the operator must NOT be able to edit/delete.
agent_msg = json.loads(srv.nth_send(channel=CH, member_id=asker, message="agent message"))["message_id"]

hub = web.EventHub(srv.DB_PATH, CH)
server = None
try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    # Operator posts a message (loopback → trusted), then edits/deletes it.
    st, resp = http(port, "/api/send", {"content": "original text"})
    if st != 200:
        check("edit/delete: loopback send accepted", False)
    else:
        own = resp["id"]
        # edit
        st, _ = http(port, "/api/edit", {"message_id": own, "content": "edited @Asker"})
        check("edit: accepted", st == 200)
        rw = row(own)
        check("edit: content updated", rw["content"] == "edited @Asker")
        check("edit: edited_at set", bool(rw["edited_at"]))
        check("edit: re-parsed @mention", asker in json.loads(rw["mentions"] or "[]"))
        # edit guards
        st, _ = http(port, "/api/edit", {"message_id": own, "content": ""})
        check("edit: empty rejected", st == 400)
        # non-string content must be a clean 400, not an AttributeError crash
        for bad in (123, [1], True, {"a": 1}):
            st, _ = http(port, "/api/edit", {"message_id": own, "content": bad})
            check(f"edit: non-string content {bad!r} -> 400", st == 400)
        st, _ = http(port, "/api/edit", {"message_id": agent_msg, "content": "hijack"})
        check("edit: agent's message rejected", st == 403)
        check("edit: agent message untouched", row(agent_msg)["content"] == "agent message")
        st, _ = http(port, "/api/edit", {"message_id": 999999, "content": "x"})
        check("edit: missing message -> 404", st == 404)

        # delete (retract)
        st, _ = http(port, "/api/delete", {"message_id": own})
        check("delete: accepted", st == 200)
        rw = row(own)
        check("delete: retracted_at set", bool(rw["retracted_at"]))
        check("delete: reason recorded", "author" in (rw["retraction_reason"] or ""))
        # synthetic [retracted #own] posted
        db = srv.get_db()
        try:
            syn = db.execute(
                "SELECT 1 FROM messages WHERE channel=? AND content LIKE ?",
                (CH, f"[retracted #{own}]%")).fetchone()
        finally:
            db.close()
        check("delete: synthetic [retracted] posted", syn is not None)
        # can't edit/delete an already-deleted message
        st, _ = http(port, "/api/edit", {"message_id": own, "content": "revive"})
        check("edit: deleted message rejected", st == 400)
        st, _ = http(port, "/api/delete", {"message_id": own})
        check("delete: double-delete rejected", st == 400)
        # can't delete the agent's message
        st, _ = http(port, "/api/delete", {"message_id": agent_msg})
        check("delete: agent's message rejected", st == 403)
except OSError as e:
    check(f"edit/delete: server started (got {e!r})", False)
finally:
    if server is not None:
        server.shutdown()
    hub.stop()

# ── Landing mode: the DEFAULT hub deployment ─────────────────────────────────
# Everything above runs single-channel, where NthWebHandler.channel holds a real
# code. The default hub runs in LANDING mode, where it is "" for every request
# and the channel comes from ?channel=. Both handlers used to bind self.channel,
# so edit and delete 404'd on every call in the mode most operators actually
# run — and nothing noticed, because no test ran in that mode.
import queue  # noqa: E402

_rl = json.loads(srv.nth_connect(summary="t", name="LandingAgent", channel="editland"))
CH_L = _rl["channel"]
hub_l = web.EventHub(srv.DB_PATH, CH_L)
server_l = None
try:
    hub_l.start()
    web.NthWebHandler.hub = None
    web.NthWebHandler.channel = ""
    web.NthWebHandler.landing_mode = True
    web.NthWebHandler.db_path = srv.DB_PATH
    server_l = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server_l.daemon_threads = True
    port_l = server_l.server_address[1]
    threading.Thread(target=server_l.serve_forever, daemon=True).start()
    time.sleep(0.2)

    st, r = http(port_l, f"/api/send?channel={CH_L}", {"content": "landing original"})
    check("landing: send accepted", st == 200)
    lid = r.get("id")

    st, _ = http(port_l, f"/api/edit?channel={CH_L}",
                 {"message_id": lid, "content": "landing edited"})
    check("landing: edit accepted (404'd before the fix)", st == 200)

    _db = srv.get_db()
    try:
        lrow = _db.execute("SELECT content, edited_at FROM messages WHERE id=?",
                           (lid,)).fetchone()
    finally:
        _db.close()
    check("landing: the edit actually applied",
          bool(lrow) and (lrow["content"] or "") == "landing edited")
    check("landing: edited_at stamped", bool(lrow) and bool(lrow["edited_at"]))

    # An edit must reach ALREADY-CONNECTED clients. The tail only selects
    # id > last_msg_id and an edit is an UPDATE, so without the message_update
    # scan every open browser renders the original text until it reloads.
    sub = hub_l.subscribe()
    time.sleep(0.3)
    while True:                      # discard the priming snapshot
        try:
            sub.get(timeout=0.2)
        except queue.Empty:
            break
    st, _ = http(port_l, f"/api/edit?channel={CH_L}",
                 {"message_id": lid, "content": "landing edited twice"})
    seen_update, deadline = None, time.time() + 6.0
    while time.time() < deadline and seen_update is None:
        try:
            ev = json.loads(sub.get(timeout=0.25))
        except queue.Empty:
            continue
        except (ValueError, TypeError):
            continue
        if ev.get("type") == "message_update" and ev.get("id") == lid:
            seen_update = ev
    hub_l.unsubscribe(sub)
    check("landing: an edit is pushed as message_update", seen_update is not None)
    check("landing: the update carries the NEW text",
          bool(seen_update) and seen_update.get("content") == "landing edited twice")

    st, _ = http(port_l, f"/api/delete?channel={CH_L}", {"message_id": lid})
    check("landing: delete accepted", st == 200)

    # Delete is a stronger promise than retract's marker: search is part of the
    # dashboard, so a deleted body must not come back from it.
    req = urllib.request.Request(
        f"http://127.0.0.1:{port_l}/api/search?channel={CH_L}&q=landing", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            sres = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sres = {"results": [{"id": lid, "http_error": e.code}]}
    hits = [x for x in (sres.get("results") or []) if x.get("id") == lid]
    check("landing: search does not return a deleted message", not hits)
except OSError as e:
    check(f"landing: server started (got {e!r})", False)
finally:
    if server_l is not None:
        server_l.shutdown()
    hub_l.stop()
    web.NthWebHandler.landing_mode = False


shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), {len(skips)} skip(s)")
sys.exit(1 if failures else 0)
