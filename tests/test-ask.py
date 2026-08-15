"""Tests for selectable answers (trio_ask multiple-choice questions).

Three tiers, all driving the REAL modules against a throwaway DB:

  1. nth_server.trio_ask + target resolution — validation, option
     normalization, human-vs-agent enforcement, stored payload shape.
  2. nth_web serialization helpers — parse_obj_json + _message_event round
     -trip (choices / selection / reply_to) against an in-memory sqlite row.
  3. Live loopback round-trip — a real nth_web server on 127.0.0.1: an agent
     asks, the (loopback-trusted) human answers via /api/send with
     reply_to + selection, and the reply is validated end to end.

Usage: python tests/test-ask.py
"""
import json
import queue
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import shutil
import sys

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv    # noqa: E402  (banner prints on import — harmless)
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


# ── Point the server at a throwaway DB ───────────────────────────────────────
_tmp = tempfile.mkdtemp(prefix="nth_ask_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def connect(name, channel="", summary="test"):
    r = json.loads(srv.nth_connect(summary=summary, name=name, channel=channel))
    assert r.get("ok"), r
    return r["channel"], r["member_id"]


def make_human(channel, name):
    """Join as a member, then mark the row kind='human' the way the web side
    does in ensure_operator_row — so trio_ask sees a real human target."""
    _ch, mid = connect(name, channel=channel)
    db = srv.get_db()
    try:
        db.execute("UPDATE members SET kind='human' WHERE id=? AND channel=?", (mid, channel))
        db.commit()
    finally:
        db.close()
    return mid


def msg_row(channel, msg_id):
    db = srv.get_db()
    try:
        return db.execute("SELECT * FROM messages WHERE id=? AND channel=?",
                          (msg_id, channel)).fetchone()
    finally:
        db.close()


# ── 1. trio_ask (nth_server) ─────────────────────────────────────────────────
CH, asker = connect("Asker", channel="asktest", summary="the agent asking")
human = make_human(CH, "Gabe")

# happy path
r = json.loads(srv.nth_ask(channel=CH, member_id=asker,
                           question="Which database?",
                           options=["Postgres", "SQLite"],
                           target=human, mode="one"))
check("ask: happy path ok", r.get("ok") is True and r.get("message_id"))
check("ask: returns resolved target name", r.get("target") == "Gabe")
qid = r.get("message_id")
row = msg_row(CH, qid)
ch = json.loads(row["choices"]) if row and row["choices"] else {}
q0 = (ch.get("questions") or [{}])[0]
check("ask: single question stored as 1-item questions list", len(ch.get("questions") or []) == 1)
check("ask: choices.mode stored", q0.get("mode") == "one")
check("ask: choices.options stored", q0.get("options") == ["Postgres", "SQLite"])
check("ask: choices.target is the human id", ch.get("target") == human)
check("ask: choices.question stored", q0.get("question") == "Which database?")
check("ask: pings the target", human in json.loads(row["mentions"] or "[]"))
check("ask: content carries a readable transcript",
      "Which database?" in (row["content"] or "") and "Postgres" in (row["content"] or ""))

# many mode
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="Pick tools",
                           options=["a", "b", "c"], target=human, mode="many"))
ch = json.loads(msg_row(CH, r["message_id"])["choices"])
check("ask: mode=many stored", ch["questions"][0].get("mode") == "many")

# batched questionnaire: multiple questions in one ask
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human, questions=[
    {"question": "Size?", "options": ["S", "M", "L"], "mode": "one"},
    {"question": "Toppings?", "options": ["Cheese", "Pepperoni"], "mode": "many", "header": "Extras"},
]))
check("ask: batch accepted", r.get("ok") is True and r.get("questions") == 2)
chb = json.loads(msg_row(CH, r["message_id"])["choices"])
check("ask: batch stores all questions", len(chb.get("questions") or []) == 2)
check("ask: batch keeps per-question mode", chb["questions"][1]["mode"] == "many")
check("ask: batch keeps header", chb["questions"][1]["header"] == "Extras")
check("ask: batch transcript lists both", "Size?" in msg_row(CH, r["message_id"])["content"]
      and "Toppings?" in msg_row(CH, r["message_id"])["content"])

# batch validation: empty list, too many, a bad question in the set
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human, questions=[]))
check("ask: empty questions list rejected", "error" in r and "non-empty" in r["error"].lower())
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human,
                           questions=[{"question": f"q{i}?", "options": ["a", "b"]}
                                      for i in range(srv.MAX_ASK_QUESTIONS + 1)]))
check("ask: too many questions rejected", "error" in r and "too many questions" in r["error"].lower())
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human, questions=[
    {"question": "ok?", "options": ["a", "b"]},
    {"question": "bad?", "options": ["only"]},   # <2 options
]))
check("ask: bad question in batch rejected (indexed)",
      "error" in r and "question 2" in r["error"].lower())

# non-dict batch item → indexed error
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human, questions=[
    {"question": "ok?", "options": ["a", "b"]}, "not-a-dict",
]))
check("ask: non-dict batch item rejected (indexed)",
      "error" in r and "question 2" in r["error"].lower())

# at-cap: exactly MAX_ASK_QUESTIONS accepted
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human,
                           questions=[{"question": f"q{i}?", "options": ["a", "b"]}
                                      for i in range(srv.MAX_ASK_QUESTIONS)]))
check("ask: exactly MAX_ASK_QUESTIONS accepted", r.get("ok") is True)

# header truncated to the cap, not rejected
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human, questions=[
    {"question": "q?", "options": ["a", "b"], "header": "H" * (srv.MAX_ASK_HEADER_LEN + 20)},
]))
chh = json.loads(msg_row(CH, r["message_id"])["choices"])
check("ask: over-long header truncated to cap",
      len(chh["questions"][0]["header"]) == srv.MAX_ASK_HEADER_LEN)

# questions wins when both singular args and questions are supplied
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human,
                           question="IGNORED", options=["x", "y"], questions=[
                               {"question": "kept1?", "options": ["a", "b"]},
                               {"question": "kept2?", "options": ["c", "d"]},
                           ]))
chw = json.loads(msg_row(CH, r["message_id"])["choices"])
check("ask: questions wins over singular args",
      len(chw["questions"]) == 2 and chw["questions"][0]["question"] == "kept1?")

# payload cap: a maxed-out batch is rejected rather than storing a ~200KB row
big = [{"question": "Q" * 2000, "options": ["O" * srv.MAX_ASK_OPTION_LEN] * 2
        + [f"x{i}" for i in range(10)]} for _ in range(srv.MAX_ASK_QUESTIONS)]
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, target=human, questions=big))
check("ask: oversized payload rejected", "error" in r and "too large" in r["error"].lower())

# agent target rejected
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["x", "y"], target=asker))
check("ask: agent target rejected", "error" in r and "agent" in r["error"].lower())

# unknown target
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["x", "y"], target="Nobody"))
check("ask: unknown target rejected", "error" in r and "no member" in r["error"].lower())

# option normalization: case-insensitive dedupe + blank drop
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["Yes", "yes", "  ", "No"], target=human))
ch = json.loads(msg_row(CH, r["message_id"])["choices"])
check("ask: dedupes case-insensitively + drops blanks",
      ch["questions"][0].get("options") == ["Yes", "No"])

# fewer than 2 distinct options
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["Only", "only"], target=human))
check("ask: <2 distinct options rejected", "error" in r and "2" in r["error"])

# too many options
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=[f"opt{i}" for i in range(srv.MAX_ASK_OPTIONS + 1)],
                           target=human))
check("ask: >max options rejected", "error" in r and "too many" in r["error"].lower())

# option too long
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["ok", "x" * (srv.MAX_ASK_OPTION_LEN + 1)], target=human))
check("ask: over-long option rejected", "error" in r and "too long" in r["error"].lower())

# empty question / bad mode
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="   ",
                           options=["a", "b"], target=human))
check("ask: empty question rejected", "error" in r and "empty" in r["error"].lower())
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["a", "b"], target=human, mode="sideways"))
check("ask: bad mode rejected", "error" in r and "mode" in r["error"].lower())

# target resolution variants
db = srv.get_db()
try:
    row_h, err = srv._resolve_human_target(db, CH, human)
    check("resolve: by member id", err is None and row_h and row_h["id"] == human)
    row_h, err = srv._resolve_human_target(db, CH, "gabe")
    check("resolve: by name (case-insensitive)", err is None and row_h and row_h["id"] == human)
    row_h, err = srv._resolve_human_target(db, CH, "ghost")
    check("resolve: unknown -> error", row_h is None and err is not None)
finally:
    db.close()

# guest-stem resolution: a 'gabe-guest' human is reachable as 'gabe'
CH2, asker2 = connect("Asker2", channel="asktest2")
guest = make_human(CH2, "gabe-guest")
r = json.loads(srv.nth_ask(channel=CH2, member_id=asker2, question="q?",
                           options=["a", "b"], target="gabe"))
check("ask: guest stem resolves target", r.get("ok") is True)

# ambiguous target (two humans share a name) → error, not a guess
CH_AMB, asker_amb = connect("AskerAmb", channel="asktestamb")
make_human(CH_AMB, "Dup")
make_human(CH_AMB, "Dup")
r = json.loads(srv.nth_ask(channel=CH_AMB, member_id=asker_amb, question="q?",
                           options=["a", "b"], target="Dup"))
check("ask: ambiguous name rejected", "error" in r and "ambiguous" in r["error"].lower())

# empty target → error
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["a", "b"], target="   "))
check("ask: empty target rejected", "error" in r and "required" in r["error"].lower())

# channel not found
r = json.loads(srv.nth_ask(channel="nosuchchan", member_id=asker, question="q?",
                           options=["a", "b"], target=human))
check("ask: channel not found rejected", "error" in r and "not found" in r["error"].lower())

# options type validation
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options="not a list", target=human))
check("ask: non-list options rejected", "error" in r and "list" in r["error"].lower())
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["ok", 5], target=human))
check("ask: non-string option rejected", "error" in r and "string" in r["error"].lower())

# at-the-cap boundaries (acceptance, not just over-cap rejection)
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=[f"o{i}" for i in range(srv.MAX_ASK_OPTIONS)], target=human))
check("ask: exactly MAX_ASK_OPTIONS accepted", r.get("ok") is True)
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="q?",
                           options=["ok", "x" * srv.MAX_ASK_OPTION_LEN], target=human))
check("ask: option exactly at length cap accepted", r.get("ok") is True)
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="x" * 2000,
                           options=["a", "b"], target=human))
check("ask: question exactly 2000 accepted", r.get("ok") is True)
r = json.loads(srv.nth_ask(channel=CH, member_id=asker, question="x" * 2001,
                           options=["a", "b"], target=human))
check("ask: question 2001 rejected", "error" in r and "too long" in r["error"].lower())

# session_token capability paths (mirror nth_send)
CH_TOK, asker_tok = connect("AskerTok", channel="asktesttok")
_conn = json.loads(srv.nth_connect(summary="tok", name="AskerTok2", channel=CH_TOK))
asker_tok2, tok2 = _conn["member_id"], _conn["session_token"]
# a fresh connect for the token owner so we have its primary token
_c = json.loads(srv.nth_connect(summary="owner", name="Owner", channel=CH_TOK))
owner, owner_tok = _c["member_id"], _c["session_token"]
htok = make_human(CH_TOK, "HumanTok")
r = json.loads(srv.nth_ask(channel=CH_TOK, member_id=owner, question="q?",
                           options=["a", "b"], target=htok, session_token=owner_tok))
check("ask: valid primary token accepted", r.get("ok") is True)
r = json.loads(srv.nth_ask(channel=CH_TOK, member_id=owner, question="q?",
                           options=["a", "b"], target=htok, session_token="bogustoken"))
check("ask: invalid token rejected", "error" in r and "invalid" in r["error"].lower())
r = json.loads(srv.nth_ask(channel=CH_TOK, member_id=owner, question="q?",
                           options=["a", "b"], target=htok, session_token=tok2))
check("ask: token/member mismatch rejected", "error" in r and "match" in r["error"].lower())
db = srv.get_db()
try:
    ro_tok = srv._mint_session_token(db, owner, CH_TOK, role="read_only")
    db.commit()
finally:
    db.close()
r = json.loads(srv.nth_ask(channel=CH_TOK, member_id=owner, question="q?",
                           options=["a", "b"], target=htok, session_token=ro_tok))
check("ask: read_only token rejected", "error" in r and "primary" in r["error"].lower())


# ── 2. nth_web serialization helpers ─────────────────────────────────────────
check("parse_obj_json: valid dict", web.parse_obj_json('{"a":1}') == {"a": 1})
check("parse_obj_json: list -> None", web.parse_obj_json('[1,2]') is None)
check("parse_obj_json: garbage -> None", web.parse_obj_json("not json") is None)
check("parse_obj_json: empty -> None", web.parse_obj_json("") is None)
check("parse_obj_json: scalar -> None", web.parse_obj_json("5") is None)

# ensure_ask_columns: a DB predating the feature (no new columns) must be
# migrated by the web side so the SSE poll doesn't crash-loop on 'no such
# column: choices'. Idempotent.
legacy = sqlite3.connect(":memory:")
legacy.execute("CREATE TABLE members (id TEXT, channel TEXT, name TEXT)")
legacy.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, channel TEXT, content TEXT)")
web.ensure_ask_columns(legacy); legacy.commit()
web.ensure_ask_columns(legacy); legacy.commit()  # idempotent, no raise
_mcols = {r[1] for r in legacy.execute("PRAGMA table_info(messages)")}
_kcols = {r[1] for r in legacy.execute("PRAGMA table_info(members)")}
check("ensure_ask_columns: adds message columns",
      {"choices", "selection", "reply_to"} <= _mcols)
check("ensure_ask_columns: adds members.kind", "kind" in _kcols)
legacy.close()

mem = sqlite3.connect(":memory:")
mem.row_factory = sqlite3.Row
mem.execute(
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, member_id TEXT, member_name TEXT, "
    "content TEXT, mentions TEXT, refs TEXT, bangs TEXT, choices TEXT, selection TEXT, "
    "reply_to INTEGER, created_at TEXT)"
)
mem.execute(
    "INSERT INTO messages VALUES (1,'a','Asker','q?','[]','','',?,'',NULL,'t')",
    (json.dumps({"mode": "one", "options": ["x", "y"], "target": "h", "question": "q?"}),),
)
mem.execute(
    "INSERT INTO messages VALUES (2,'h','Gabe','x','','','','',?,1,'t')",
    (json.dumps({"picked": [0], "custom": ""}),),
)
q_ev = web._message_event(mem, mem.execute("SELECT * FROM messages WHERE id=1").fetchone())
a_ev = web._message_event(mem, mem.execute("SELECT * FROM messages WHERE id=2").fetchone())
check("event: question carries choices dict", isinstance(q_ev["choices"], dict)
      and q_ev["choices"]["options"] == ["x", "y"])
check("event: question has no selection", q_ev["selection"] is None)
check("event: answer carries selection dict", a_ev["selection"] == {"picked": [0], "custom": ""})
check("event: answer carries reply_to", a_ev["reply_to"] == 1)
# Older-row path: a SELECT lacking the new columns must not raise KeyError —
# choices/selection/reply_to fall back to None.
old_row = mem.execute(
    "SELECT id, member_id, member_name, content, mentions, refs, bangs, created_at "
    "FROM messages WHERE id=1").fetchone()
old_ev = web._message_event(mem, old_row)
check("event: older row (no new columns) -> None fields, no crash",
      old_ev["choices"] is None and old_ev["selection"] is None and old_ev["reply_to"] is None)
mem.close()


# ── 3. Live loopback round-trip (nth_web /api/send answer path) ──────────────
def http(server_port, path, method="GET", body=None):
    url = f"http://127.0.0.1:{server_port}{path}"
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


CH3, asker3 = connect("LiveAsker", channel="asktest3")
hub = web.EventHub(srv.DB_PATH, CH3)
server = None
try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH3
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    # First send from loopback creates the trusted human operator row.
    st, hello_resp = http(port, "/api/send", "POST", {"content": "hello from the human"})
    hello_id = hello_resp.get("id")
    if st != 200:
        check("live round-trip: loopback send accepted", False)
    else:
        # The operator is the kind='human' member in this channel.
        db = srv.get_db()
        try:
            hrow = db.execute("SELECT id FROM members WHERE channel=? AND kind='human'",
                              (CH3,)).fetchone()
        finally:
            db.close()
        check("live: loopback operator marked human", hrow is not None)
        human3 = hrow["id"]

        # Agent asks the human.
        r = json.loads(srv.nth_ask(channel=CH3, member_id=asker3,
                                   question="Ship it?", options=["Yes", "No"],
                                   target=human3, mode="one"))
        check("live: ask accepted for loopback human", r.get("ok") is True)
        qid3 = r["message_id"]

        # Human answers via the picker's POST shape (answers list, one entry).
        st, resp = http(port, "/api/send", "POST",
                        {"content": "Yes", "reply_to": qid3,
                         "selection": {"answers": [{"picked": [0], "custom": []}]}})
        check("live: answer send accepted", st == 200 and resp.get("ok"))
        arow = msg_row(CH3, resp.get("id")) if resp.get("id") else None
        check("live: answer row links reply_to", arow and arow["reply_to"] == qid3)
        sel = json.loads(arow["selection"]) if arow and arow["selection"] else {}
        check("live: answer row stores selection",
              (sel.get("answers") or [{}])[0].get("picked") == [0])

        # Batched questionnaire: answer a 2-question ask in one submit.
        rb = json.loads(srv.nth_ask(channel=CH3, member_id=asker3, target=human3, questions=[
            {"question": "Size?", "options": ["S", "M"], "mode": "one"},
            {"question": "Toppings?", "options": ["Cheese", "Pepperoni"], "mode": "many"},
        ]))
        qidb = rb["message_id"]
        st, resp = http(port, "/api/send", "POST",
                        {"content": "Size? → M\nToppings? → Cheese, Pepperoni",
                         "reply_to": qidb,
                         "selection": {"answers": [{"picked": [1], "custom": []},
                                                   {"picked": [0, 1], "custom": ["Olives"]}]}})
        check("live: batch answer accepted", st == 200 and resp.get("ok"))
        # answer-count mismatch on a FRESH batch → 400
        rb2 = json.loads(srv.nth_ask(channel=CH3, member_id=asker3, target=human3, questions=[
            {"question": "A?", "options": ["1", "2"]},
            {"question": "B?", "options": ["3", "4"]},
        ]))
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": rb2["message_id"],
                      "selection": {"answers": [{"picked": [0], "custom": []}]}})
        check("live: batch answer-count mismatch -> 400", st == 400)

        # per-question bounds: Q1 valid, Q2 index out of range → 400 (rb2 still
        # unanswered after the rejected mismatch above).
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": rb2["message_id"],
                      "selection": {"answers": [{"picked": [0], "custom": []},
                                                {"picked": [9], "custom": []}]}})
        check("live: batch per-question bounds (Q2 OOR) -> 400", st == 400)

        # already-answered on a BATCH → 409 (qidb answered above).
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": qidb,
                      "selection": {"answers": [{"picked": [0], "custom": []},
                                                {"picked": [0], "custom": []}]}})
        check("live: second answer to a batch -> 409", st == 409)

        # single-select cardinality: a mode="one" question rejects >1 pick.
        rc = json.loads(srv.nth_ask(channel=CH3, member_id=asker3, target=human3,
                                    question="One?", options=["a", "b", "c"], mode="one"))
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": rc["message_id"],
                      "selection": {"answers": [{"picked": [0, 1], "custom": []}]}})
        check("live: single-select >1 pick -> 400", st == 400)

        # empty sub-answer (no pick, no text) → 400.
        rd = json.loads(srv.nth_ask(channel=CH3, member_id=asker3, target=human3,
                                    question="E?", options=["a", "b"]))
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": rd["message_id"],
                      "selection": {"answers": [{"picked": [], "custom": []}]}})
        check("live: empty sub-answer -> 400", st == 400)

        # oversized answers list (>20) → 400.
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": rd["message_id"],
                      "selection": {"answers": [{"picked": [0], "custom": []}] * 21}})
        check("live: >20 answers -> 400", st == 400)

        # non-string custom entry → 400.
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": rd["message_id"],
                      "selection": {"answers": [{"picked": [], "custom": [123]}]}})
        check("live: non-string custom -> 400", st == 400)

        # legacy single-answer selection shape (no 'answers' key) is refused.
        st, _ = http(port, "/api/send", "POST",
                     {"content": "a", "reply_to": rd["message_id"],
                      "selection": {"picked": [0], "custom": ""}})
        check("live: legacy {picked} selection shape -> 400", st == 400)

        # reply_to type/range guards.
        for bad in (0, -1, 1.5, "3"):
            st, _ = http(port, "/api/send", "POST",
                         {"content": "x", "reply_to": bad,
                          "selection": {"answers": [{"picked": [0], "custom": []}]}})
            check(f"live: reply_to={bad!r} -> 400", st == 400)

        # Answer-path invariants (the LOTC hardening).
        # (a) already-answered → 409 (qid3 was answered above).
        st, _ = http(port, "/api/send", "POST",
                     {"content": "No", "reply_to": qid3,
                      "selection": {"answers": [{"picked": [1], "custom": []}]}})
        check("live: second answer to same question -> 409", st == 409)

        # (b) selection on a non-question message → 400.
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": hello_id,
                      "selection": {"answers": [{"picked": [0], "custom": []}]}})
        check("live: selection on non-ask message -> 400", st == 400)

        # (c) answering a question addressed to someone else → 403.
        db = srv.get_db()
        try:
            db.execute("INSERT INTO members (id, channel, name, kind, joined_at) "
                       "VALUES ('other-human-xyz', ?, 'Other', 'human', ?)",
                       (CH3, srv.now_iso()))
            db.commit()
        finally:
            db.close()
        r = json.loads(srv.nth_ask(channel=CH3, member_id=asker3, question="For Other?",
                                   options=["A", "B"], target="other-human-xyz"))
        qid_other = r["message_id"]
        st, _ = http(port, "/api/send", "POST",
                     {"content": "A", "reply_to": qid_other,
                      "selection": {"answers": [{"picked": [0], "custom": []}]}})
        check("live: answering someone else's question -> 403", st == 403)

        # (d) picked index out of range → 400.
        r = json.loads(srv.nth_ask(channel=CH3, member_id=asker3, question="Pick",
                                   options=["A", "B"], target=human3))
        st, _ = http(port, "/api/send", "POST",
                     {"content": "?", "reply_to": r["message_id"],
                      "selection": {"answers": [{"picked": [99], "custom": []}]}})
        check("live: selection.picked out of range -> 400", st == 400)

        # Negative validation (request-shape).
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "selection": {"answers": [{"picked": [0], "custom": []}]}})
        check("live: selection without reply_to -> 400", st == 400)
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": 999999})
        check("live: reply_to to missing message -> 400", st == 400)
        st, _ = http(port, "/api/send", "POST",
                     {"content": "x", "reply_to": qid3,
                      "selection": {"answers": [{"picked": ["nope"], "custom": []}]}})
        check("live: non-int selection.picked -> 400", st == 400)
except OSError as e:
    check(f"live round-trip: server started (got {e!r})", False)
finally:
    if server is not None:
        server.shutdown()
    hub.stop()


# ── 4. The EventHub actually SELECTS the ask columns ─────────────────────────
# Tier 3 above starts a real EventHub but never consumes an event from it —
# every assertion there reads the row back from the DB. So nothing proved the
# hub's own SQL carries choices/selection/reply_to, and dropping those three
# columns from both of its SELECTs left the whole suite green. The dashboard is
# the ONLY consumer of these fields, and the hub is its only supply.
#
# Both delivery paths are covered, because they are separate queries:
#   * the reconnect burst  — _prime_subscriber's snapshot, for a client that
#     connects when the ask and its answer are already in history;
#   * the live tail        — the poll loop, for a client already connected when
#     the answer arrives.
def drain(q, timeout=1.5):
    """Collect whatever is already queued, then stop."""
    out, deadline = [], time.time() + timeout
    while time.time() < deadline:
        try:
            out.append(q.get(timeout=0.2))
        except queue.Empty:
            break
    return out


def drain_until(q, want_id, timeout=6.0):
    """Collect events until `want_id` appears, or time out.

    NOT "until the queue goes quiet". The hub polls on an interval, so a
    quiet-gap heuristic returns during the gap BEFORE the tick that carries
    the message — the assertion then fails for a reason that has nothing to do
    with the code under test. Wait for the thing being asserted on."""
    out, deadline = [], time.time() + timeout
    while time.time() < deadline:
        if want_id in ask_events(out):
            break
        try:
            out.append(q.get(timeout=0.25))
        except queue.Empty:
            continue
    return out


def ask_events(evs):
    """Message events keyed by id. The hub queues JSON STRINGS, not dicts."""
    by_id = {}
    for ev in evs:
        try:
            payload = json.loads(ev) if isinstance(ev, str) else ev
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        # A snapshot carries a list; the live tail carries one message. Accept
        # every shape the hub emits rather than pinning one, so this test keeps
        # working if the envelope changes but the COLUMNS are what regress.
        candidates = []
        for key in ("messages", "history"):
            if isinstance(payload.get(key), list):
                candidates.extend(payload[key])
        for key in ("message", "msg"):
            if isinstance(payload.get(key), dict):
                candidates.append(payload[key])
        if payload.get("id") and "content" in payload:
            candidates.append(payload)
        for m in candidates:
            if isinstance(m, dict) and m.get("id"):
                by_id[m["id"]] = m
    return by_id


CH4, asker4 = connect("HubAsker", channel="asktest4")
hub4 = web.EventHub(srv.DB_PATH, CH4)
server4 = None
try:
    hub4.start()
    web.NthWebHandler.hub = hub4
    web.NthWebHandler.channel = CH4
    web.NthWebHandler.db_path = srv.DB_PATH
    server4 = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server4.daemon_threads = True
    port4 = server4.server_address[1]
    threading.Thread(target=server4.serve_forever, daemon=True).start()
    time.sleep(0.2)

    st, _ = http(port4, "/api/send", "POST", {"content": "hub tier: hello"})
    check("hub: loopback send accepted", st == 200)
    _db4 = srv.get_db()
    try:
        _h4 = _db4.execute(
            "SELECT id FROM members WHERE channel=? AND kind='human'",
            (CH4,)).fetchone()
    finally:
        _db4.close()
    check("hub: loopback operator marked human", _h4 is not None)
    human4 = _h4["id"] if _h4 else ""

    q_ask = json.loads(srv.nth_ask(channel=CH4, member_id=asker4, target=human4,
                                   question="Ship it?", options=["Yes", "No"],
                                   mode="one"))
    qid4 = q_ask.get("message_id")
    check("hub: ask posted", bool(qid4))

    # --- live tail: subscribe FIRST, then answer ---
    live_q = hub4.subscribe()
    drain(live_q)                      # discard the priming snapshot
    st, ans = http(port4, "/api/send", "POST", {
        "content": "Yes — ship it",
        "reply_to": qid4,
        "selection": {"answers": [{"picked": [0], "custom": []}]}})
    check("hub: answer accepted", st == 200)
    aid4 = ans.get("id")
    live = ask_events(drain_until(live_q, aid4))
    hub4.unsubscribe(live_q)

    live_ans = live.get(aid4)
    check("hub live tail: answer event delivered", live_ans is not None)
    check("hub live tail: event carries reply_to",
          bool(live_ans) and live_ans.get("reply_to") == qid4)
    check("hub live tail: event carries selection",
          bool(live_ans) and bool(live_ans.get("selection")))

    # --- reconnect burst: subscribe AFTER, read the snapshot ---
    burst_q = hub4.subscribe()
    burst = ask_events(drain(burst_q))
    hub4.unsubscribe(burst_q)

    burst_ask = burst.get(qid4)
    burst_ans = burst.get(aid4)
    check("hub reconnect burst: the ask is in the snapshot", burst_ask is not None)
    check("hub reconnect burst: the ask carries its choices",
          bool(burst_ask) and bool(burst_ask.get("choices")))
    check("hub reconnect burst: the answer carries selection + reply_to",
          bool(burst_ans) and bool(burst_ans.get("selection"))
          and burst_ans.get("reply_to") == qid4)
except OSError as e:
    check(f"hub tier: server started (got {e!r})", False)
finally:
    if server4 is not None:
        server4.shutdown()
    hub4.stop()


shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), {len(skips)} skip(s)")
sys.exit(1 if failures else 0)
