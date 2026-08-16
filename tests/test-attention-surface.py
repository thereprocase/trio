"""GET /api/questions, /api/mentions, /api/tasks — the "what needs me" surface.

Each of these is a queue, and the interesting property of a queue is what
leaves it and what never enters it:

  * a question is PENDING until the operator answers it, and "answered" is
    derived from the reply rather than stored as a flag, so an answer sent
    from anywhere clears it everywhere;
  * a question posed to somebody else is not in the operator's queue;
  * the mentions LIKE filter is a coarse prefilter — an id that merely
    CONTAINS the operator's must not count as a mention of them;
  * an archived channel's items leave all three queues, which is most of the
    point of archiving.

Usage: python tests/test-attention-surface.py
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


_tmp = tempfile.mkdtemp(prefix="nth_attn_")
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
        except Exception:                                   # noqa: BLE001
            return e.code, {}


def raw_message(channel, sender, name, content, **cols):
    db = srv.get_db()
    try:
        keys = ["channel", "member_id", "member_name", "content", "created_at"]
        vals = [channel, sender, name, content, srv.now_iso()]
        for k, v in cols.items():
            keys.append(k)
            vals.append(v)
        cur = db.execute(
            f"INSERT INTO messages ({','.join(keys)}) "
            f"VALUES ({','.join('?' * len(keys))})", vals)
        db.commit()
        return cur.lastrowid
    finally:
        db.close()


CH, alice = connect("Alice", channel="attn")
_c, bob = connect("Bob", channel=CH)

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
        oprow = db.execute(
            "SELECT id, name FROM members WHERE channel=? "
            "AND id LIKE '\\_op\\_%' ESCAPE '\\'", (CH,)).fetchone()
        op, op_name = oprow[0], oprow[1]
    finally:
        db.close()

    # ── questions ───────────────────────────────────────────────────────────
    q_mine = raw_message(
        CH, alice, "Alice", "Ship it?",
        choices=json.dumps({"target": op, "mode": "one",
                            "question": "Ship it?",
                            "options": ["yes", "no"]}))
    raw_message(
        CH, alice, "Alice", "Bob, which branch?",
        choices=json.dumps({"target": bob, "mode": "one",
                            "question": "Which branch?",
                            "options": ["main", "dev"]}))

    st, body = http(port, "/api/questions")
    check("questions: answers 200", st == 200 and body.get("ok") is True)
    ids = {q["id"] for q in body["questions"]}
    check("questions: a question addressed to the operator is pending",
          q_mine in ids)
    check("questions: a question addressed to SOMEONE ELSE is not in the "
          "operator's queue", len(ids) == 1)
    check("questions: the option list travels with it",
          body["questions"][0]["questions"][0]["options"] == ["yes", "no"])

    # Answering is a reply carrying a selection — derived, not a stored flag.
    raw_message(CH, op, op_name, "yes", reply_to=q_mine,
                selection=json.dumps({"picked": ["yes"]}))
    _st, body2 = http(port, "/api/questions")
    check("questions: answering it removes it from the queue",
          q_mine not in {q["id"] for q in body2["questions"]})

    # A reply with no selection is just a comment, not an answer.
    q2 = raw_message(
        CH, alice, "Alice", "And deploy?",
        choices=json.dumps({"target": op, "mode": "one",
                            "question": "Deploy?", "options": ["y", "n"]}))
    raw_message(CH, op, op_name, "hmm let me think", reply_to=q2)
    _st, body3 = http(port, "/api/questions")
    check("questions: a reply WITHOUT a selection does not count as answering",
          q2 in {q["id"] for q in body3["questions"]})

    # ── mentions ────────────────────────────────────────────────────────────
    m1 = raw_message(CH, alice, "Alice", f"@{op_name} take a look",
                     mentions=json.dumps([op]))
    # An id that merely CONTAINS the operator's passes the LIKE prefilter.
    raw_message(CH, alice, "Alice", "not for you",
                mentions=json.dumps([op + "_lookalike"]))
    # Your own mention of yourself is not an inbox item.
    raw_message(CH, op, op_name, f"@{op_name} note to self",
                mentions=json.dumps([op]))

    st_m, mb = http(port, "/api/mentions")
    got = {m["id"] for m in mb["mentions"]}
    check("mentions: answers 200", st_m == 200 and mb.get("ok") is True)
    check("mentions: a real mention is listed", m1 in got)
    check("mentions: an id that merely CONTAINS the operator's id is not a "
          "mention of them — the LIKE is only a prefilter", len(got) == 1)
    check("mentions: unread_count tracks the unread ones",
          mb["unread_count"] == 1)
    check("mentions: each carries a read receipt",
          mb["mentions"][0]["read"] is False)

    http(port, "/api/messages/mark-read", "POST", {"ids": [m1]})
    _st, mb2 = http(port, "/api/mentions")
    check("mentions: marking read flips the receipt and clears the count",
          mb2["mentions"][0]["read"] is True and mb2["unread_count"] == 0)

    # ── tasks ───────────────────────────────────────────────────────────────
    t1 = json.loads(srv.nth_send(channel=CH, member_id=alice,
                                 message="do the thing", task=True))["task_id"]
    t2 = json.loads(srv.nth_send(channel=CH, member_id=alice,
                                 message="second thing", task=True))["task_id"]
    srv.nth_claim(channel=CH, member_id=bob, task_id=t2)

    st_t, tb = http(port, "/api/tasks")
    check("tasks: answers 200", st_t == 200 and tb.get("ok") is True)
    check("tasks: lists this channel's tasks", tb["count"] == 2)
    check("tasks: open sorts before claimed",
          [t["status"] for t in tb["tasks"]] == ["open", "claimed"])
    check("tasks: a claimed task names its owner",
          [t for t in tb["tasks"] if t["id"] == t2][0]["claimed_by"] == bob)
    check("tasks: blocked_by comes back as a list, not a JSON string",
          isinstance(tb["tasks"][0]["blocked_by"], list))
    check("tasks: reports the channel it answered for", tb["channel"] == CH)
    _ = t1

    # ── archiving a channel empties all three queues ────────────────────────
    http(port, "/api/archives", "POST",
         {"kind": "channel", "key": CH, "archived": True})
    _st, aq = http(port, "/api/questions")
    _st, am = http(port, "/api/mentions")
    check("archive: an archived channel's questions leave the queue",
          aq["count"] == 0)
    check("archive: and its mentions leave too", am["count"] == 0)
    http(port, "/api/archives", "POST",
         {"kind": "channel", "key": CH, "archived": False})

    # ── operator gate ───────────────────────────────────────────────────────
    _real = web.NthWebHandler._resolve_identity
    try:
        class _Guest:
            source = web.IDENTITY_SOURCE_GUEST
            name = "guest"
            summary = "guest"
            member_id = "guest_intruder"
            display_name = "guest"
        web.NthWebHandler._resolve_identity = lambda self: (None, _Guest(), False)
        st_gq, _ = http(port, "/api/questions")
        st_gm, body_gm = http(port, "/api/mentions")
        check("authz: a guest cannot read the question queue", st_gq == 403)
        check("authz: a guest cannot read the mention queue", st_gm == 403)
        check("authz: and no message content leaks in the refusal",
              "take a look" not in json.dumps(body_gm))
    finally:
        web.NthWebHandler._resolve_identity = _real

    # A pending identity has not identified itself yet; the task board is
    # channel-scoped rather than operator-only, so it uses that gate.
    _real = web.NthWebHandler._resolve_identity
    try:
        class _Pending:
            source = web.IDENTITY_SOURCE_PENDING
            name = "pending"
            summary = "pending"
            member_id = "pending_x"
            display_name = "pending"
        web.NthWebHandler._resolve_identity = lambda self: (None, _Pending(), False)
        st_tp, _ = http(port, "/api/tasks")
        check("authz: the task board refuses an unidentified caller",
              st_tp == 403)
    finally:
        web.NthWebHandler._resolve_identity = _real
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
print("OK — questions clear when answered, mentions match exactly, "
      "tasks sort by urgency, and archiving empties the queues")
