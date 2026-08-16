"""Regression tests for the defects the LOTC review found.

Each test here corresponds to a bug that shipped green through the original
suite, so each one is written to fail against the code as it was:

  * unread was counted over "the newest 500 messages", which made the count
    WRONG rather than approximate — reading the newest 500 reported 0 while
    older messages were still unread (Sauron, proven);
  * /api/dms windowed 2000 rows GLOBALLY, so agent-to-agent traffic evicted
    the operator's own inbox entirely (Sauron, proven);
  * the ?with= archive fallback split a "group:a,b" key on "," and got
    ["group:a", "b"], silently mis-deriving group threads (Sauron, proven);
  * restoring an agent-archived DM returned ok/archived:false while the thread
    stayed hidden, and the payload could not express archived-by-you AND
    archived-because-the-agent-was (Frodo);
  * delete_channel on an unknown channel reported success (Frodo);
  * POST /api/channels crashed on a non-string code (Uruk-Hai).

Usage: python tests/test-review-fixes.py
"""
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_rev_")
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:                                   # noqa: BLE001
            return e.code, {}


def bulk_messages(channel, sender, name, n, recipients=None):
    db = srv.get_db()
    try:
        rec = json.dumps(recipients) if recipients else "[]"
        db.executemany(
            "INSERT INTO messages (channel, member_id, member_name, content, "
            "recipients, created_at) VALUES (?,?,?,?,?,?)",
            [(channel, sender, name, f"bulk {i}", rec, srv.now_iso())
             for i in range(n)])
        db.commit()
    finally:
        db.close()


CH, alice = connect("Alice", channel="revfix")
_c, carol = connect("Carol", channel=CH)
OTHER, bob = connect("Bob", channel="revother")

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
    http(port, "/api/send", "POST", {"content": "operator here"})
    db = srv.get_db()
    try:
        op = db.execute(
            "SELECT id FROM members WHERE channel=? "
            "AND id LIKE '\\_op\\_%' ESCAPE '\\'", (CH,)).fetchone()[0]
    finally:
        db.close()

    # ── 1. unread must not report 0 while messages are unread ───────────────
    CAP = web.UNREAD_COUNT_CAP
    bulk_messages(CH, alice, "Alice", CAP + 100)
    _st, b = http(port, "/api/channels")
    row = {c["code"]: c for c in b["channels"]}[CH]
    check(f"unread: a backlog past the cap reports the cap, flagged "
          f"({row['unread']}, capped={row['unread_capped']})",
          row["unread"] == CAP and row["unread_capped"] is True)

    # Read the newest CAP messages. The OLD code reported 0 here, because the
    # candidate set was the newest CAP rows and all of them were now read.
    db = srv.get_db()
    try:
        newest = [r[0] for r in db.execute(
            "SELECT id FROM messages WHERE channel=? ORDER BY id DESC LIMIT ?",
            (CH, CAP)).fetchall()]
    finally:
        db.close()
    for i in range(0, len(newest), 500):
        http(port, "/api/messages/mark-read", "POST",
             {"ids": newest[i:i + 500]})
    _st, b2 = http(port, "/api/channels")
    row2 = {c["code"]: c for c in b2["channels"]}[CH]
    check("unread: after reading the newest batch, the OLDER unread are still "
          f"counted — not silently zero (reported {row2['unread']})",
          row2["unread"] > 0)

    # ── 2. the operator's DM inbox must not be evicted by agent traffic ─────
    db = srv.get_db()
    try:
        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, "
            "recipients, created_at) VALUES (?,?,?,?,?,?)",
            (CH, alice, "Alice", "a private word for the operator",
             json.dumps([op]), srv.now_iso()))
        db.commit()
    finally:
        db.close()
    _st, d1 = http(port, "/api/dms")
    check("dms: the operator's own thread is listed",
          alice in {t["key"] for t in d1["your_dms"]})

    # Bury it under agent-to-agent DM traffic far exceeding the window.
    bulk_messages(CH, bob, "Bob", 2100, recipients=[carol])
    _st, d2 = http(port, "/api/dms")
    check("dms: the operator's thread SURVIVES 2100 unrelated agent DMs — "
          "the window is per-owner, not global",
          alice in {t["key"] for t in d2["your_dms"]})
    _st, d3 = http(port, f"/api/dms?with={alice}")
    check("dms: and its history is still readable",
          any("private word" in m["content"] for m in d3["messages"]))

    # ── 3. group thread keys must parse correctly ───────────────────────────
    check("keys: a group key's participants parse without the prefix",
          web.participants_in_key("group:" + ",".join(sorted([alice, carol])))
          == sorted([alice, carol]))
    check("keys: a 1:1 key is a single participant",
          web.participants_in_key(alice) == [alice])
    check("keys: an audit key splits on commas",
          web.participants_in_key(f"{bob},{carol}") == [bob, carol])
    check("keys: an empty key has no participants",
          web.participants_in_key("") == [])

    # ── 4. archive state must be expressible and honestly reported ──────────
    db = srv.get_db()
    try:
        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, "
            "recipients, created_at) VALUES (?,?,?,?,?,?)",
            (CH, carol, "Carol", "carol says hi",
             json.dumps([op]), srv.now_iso()))
        db.commit()
    finally:
        db.close()
    http(port, "/api/archives", "POST",
         {"kind": "dm", "key": carol, "archived": True})
    _st, d4 = http(port, "/api/dms?archived=1")
    t_carol = {t["key"]: t for t in d4["your_dms"]}.get(carol, {})
    check("archive: a self-archived thread says so explicitly",
          t_carol.get("self_archived") is True
          and t_carol.get("agent_archived") is False)

    # Now archive the AGENT too — both reasons hold at once.
    db = srv.get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO agents (id, name, model, base_prompt, "
            "state, managed, created_at, last_active_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (carol, "Carol", "m", "", "stopped", 1,
             srv.now_iso(), srv.now_iso()))
        db.execute("UPDATE agents SET archived_at=? WHERE id=?",
                   (srv.now_iso(), carol))
        db.commit()
    finally:
        db.close()
    _st, d5 = http(port, "/api/dms?archived=1")
    t2 = {t["key"]: t for t in d5["your_dms"]}.get(carol, {})
    check("archive: BOTH causes are reported independently — the payload can "
          "describe a thread archived by you AND by its agent",
          t2.get("self_archived") is True and t2.get("agent_archived") is True)

    # Restoring clears only YOUR archive; the server must say the thread is
    # still hidden rather than claim success.
    st_r, r_body = http(port, "/api/archives", "POST",
                        {"kind": "dm", "key": carol, "archived": False})
    check("archive: restore reports the EFFECTIVE state, not the requested "
          "one — it does not claim the thread is back when it is not",
          st_r == 200 and r_body.get("archived") is True
          and r_body.get("agent_archived") is True)
    check("archive: and explains what to do about it",
          "agent" in (r_body.get("note") or "").lower())
    _st, d6 = http(port, "/api/dms?archived=0")
    check("archive: the thread is indeed still not in the active list",
          carol not in {t["key"] for t in d6["your_dms"]})

    # ── 5. delete_channel must refuse an unknown channel ────────────────────
    st_d, _ = http(port, "/api/prune", "POST",
                   {"action": "delete_channel", "channel": "no-such-chan",
                    "dry_run": False})
    check("prune: deleting a channel that does not exist is a 404, not a "
          "cheerful success", st_d == 404)

    # ── 6. channel create must not crash on a non-string ────────────────────
    for bad in (12345, ["a"], {"x": 1}, True):
        st_b, _ = http(port, "/api/channels", "POST", {"code": bad})
        check(f"create: a non-string code ({type(bad).__name__}) is a 400, "
              "not a dropped connection", st_b == 400)
    st_t, _ = http(port, "/api/channels", "POST", {"topic": 99})
    check("create: a non-string topic is a 400", st_t == 400)
    st_ok, ok_body = http(port, "/api/channels", "POST",
                          {"topic": "Deploy Plan"})
    check("create: a normal request still works",
          st_ok == 201 and ok_body["channel"]["code"] == "deploy-plan")
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    hub.stop()
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("OK — every defect the review proved is now pinned by a test")
