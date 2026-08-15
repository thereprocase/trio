"""Tests for REAL private DMs — the member-aware visibility engine.

Covers the locked design:
  (a) a recipient sees a DM via trio_poll and trio_history
  (b) a NON-recipient does NOT see it via poll, history, pounds, the SSE
      message event, AND the monitor unread query
  (c) the operator (kind != 'agent' / _op_ id) is all-seeing — sees every DM
  (d) broadcasts still reach everyone (no regression)
  (e) a hidden DM still advances the non-recipient's watermark (not re-surfaced)
  + the dashboard /api/send DM path stores recipients and the server enforces it.

Usage: python tests/test-dms.py
"""
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
import shutil
import sqlite3
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv          # noqa: E402
import nth_web as web            # noqa: E402
from nth_constants import AGENT_INBOX_CHANNEL, can_see, is_all_seeing  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_dms_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def _poll_one(channel, member_id):
    r = json.loads(srv.nth_poll(channel=channel, member_id=member_id, wait_seconds=0))
    return r.get("messages", []) if r.get("event") == "new_messages" else []


def poll_ids(member_id, contents=None):
    """Return topic + global-inbox messages visible to this member."""
    msgs = _poll_one(CH, member_id) + _poll_one(AGENT_INBOX_CHANNEL, member_id)
    return [m["id"] for m in msgs], [m["content"] for m in msgs]


def history_ids(member_id=""):
    topic = json.loads(srv.nth_history(channel=CH, last_n=100, member_id=member_id))
    inbox = json.loads(srv.nth_history(
        channel=AGENT_INBOX_CHANNEL, last_n=100, member_id=member_id))
    return [m["id"] for m in topic.get("messages", [])] + [
        m["id"] for m in inbox.get("messages", [])]


def watermark(member_id):
    db = srv.get_db()
    try:
        row = db.execute(
            "SELECT last_read FROM members WHERE channel=? AND id=?",
            (AGENT_INBOX_CHANNEL, member_id)).fetchone()
        return row["last_read"] if row else None
    finally:
        db.close()


def dm_row(msg_id):
    db = srv.get_db()
    try:
        return db.execute(
            "SELECT channel, member_id, recipients, mentions FROM messages WHERE id=?",
            (msg_id,)).fetchone()
    finally:
        db.close()


def message_exists_in_channel(msg_id, channel):
    db = srv.get_db()
    try:
        return db.execute(
            "SELECT 1 FROM messages WHERE id=? AND channel=?",
            (msg_id, channel)).fetchone() is not None
    finally:
        db.close()


# ── Roster: Alice (sender), Bob (recipient), Carol (non-recipient) ──
A = json.loads(srv.nth_connect(summary="a", name="Alice", channel="dmtest"))
CH = A["channel"]
alice = A["member_id"]
bob = json.loads(srv.nth_connect(summary="b", name="Bob", channel=CH))["member_id"]
carol = json.loads(srv.nth_connect(summary="c", name="Carol", channel=CH))["member_id"]

# Everyone reads the channel up to now so join chatter doesn't confound polls.
srv.nth_poll(channel=CH, member_id=bob, wait_seconds=0)
srv.nth_poll(channel=CH, member_id=carol, wait_seconds=0)

# ── Alice DMs Bob ──
dm = json.loads(srv.nth_dm(channel=CH, member_id=alice, message="secret-for-bob", to="Bob"))
DM_ID = dm["message_id"]
check("trio_dm: ok", dm.get("ok") is True)
check("trio_dm: new DM row is in the global inbox",
      dm_row(DM_ID)["channel"] == AGENT_INBOX_CHANNEL)
check("trio_dm: new DM is absent from the topic channel",
      not message_exists_in_channel(DM_ID, CH))
check("trio_dm: recipients resolved to Bob", dm.get("recipients") == [bob])
row = dm_row(DM_ID)
check("trio_dm: recipients stored on row", json.loads(row["recipients"]) == [bob])
check("trio_dm: recipient auto-added to ping set", bob in json.loads(row["mentions"] or "[]"))

# unresolved recipient is rejected (no silent broadcast)
bad = json.loads(srv.nth_dm(channel=CH, member_id=alice, message="x", to="Nobody"))
check("trio_dm: unknown recipient rejected", "error" in bad)
# empty `to` rejected
bad2 = json.loads(srv.nth_dm(channel=CH, member_id=alice, message="x", to=""))
check("trio_dm: empty `to` rejected", "error" in bad2)

# (a) recipient sees the DM via poll + history
bob_ids, bob_contents = poll_ids(bob)
check("(a) recipient sees DM via poll", DM_ID in bob_ids)
check("(a) recipient poll carries DM body", "secret-for-bob" in bob_contents)
check("(a) recipient sees DM via history", DM_ID in history_ids(bob))

# (b) non-recipient does NOT see the DM
carol_ids, carol_contents = poll_ids(carol)
check("(b) non-recipient does NOT see DM via poll", DM_ID not in carol_ids)
check("(b) non-recipient poll has no DM body", "secret-for-bob" not in carol_contents)
check("(b) non-recipient does NOT see DM via history (with id)", DM_ID not in history_ids(carol))
check("(b) unidentified history sees broadcasts only (no DM)", DM_ID not in history_ids(""))

# (b) pounds: a #ref to a non-recipient inside a DM must not surface it.
#     Alice DMs Bob but #references Carol — Carol must NOT see it via pounds.
dm2 = json.loads(srv.nth_dm(channel=CH, member_id=alice, message="re #Carol, tell nobody", to="Bob"))
DM2_ID = dm2["message_id"]
carol_pounds = json.loads(srv.nth_pounds(channel=AGENT_INBOX_CHANNEL, member_id=carol))
check("(b) non-recipient does NOT see referenced DM via pounds",
      DM2_ID not in [m["id"] for m in carol_pounds.get("messages", [])])
# Bob (a recipient) who is #referenced would see it; sanity: Bob referenced?
dm3 = json.loads(srv.nth_dm(channel=CH, member_id=alice, message="hey #Bob look", to="Bob"))
DM3_ID = dm3["message_id"]
bob_pounds = json.loads(srv.nth_pounds(channel=AGENT_INBOX_CHANNEL, member_id=bob))
check("(b) recipient DOES see their referenced DM via pounds",
      DM3_ID in [m["id"] for m in bob_pounds.get("messages", [])])

# (b) SSE message event carries recipients (real data) and the predicate the
#     delivery paths use withholds the DM from Carol. The dashboard itself is
#     the operator's all-seeing surface, so its live feed ships every row; the
#     recipients field is what makes the DM tab a REAL (server-backed) scope.
db = srv.get_db()
try:
    sse_row = db.execute(
        "SELECT id, member_id, member_name, content, mentions, refs, bangs, "
        "reply_to, recipients, retracted_at, retraction_reason, "
        "created_at FROM messages WHERE id=?", (DM_ID,)).fetchone()
    ev = web._message_event(db, sse_row)
finally:
    db.close()
check("(b) SSE event carries recipients", ev.get("recipients") == [bob])
check("(b) predicate over SSE row withholds DM from non-recipient",
      can_see(carol, "agent", ev["member_id"], json.dumps(ev["recipients"])) is False)
check("(b) predicate over SSE row admits DM for recipient",
      can_see(bob, "agent", ev["member_id"], json.dumps(ev["recipients"])) is True)

# (b) monitor unread query: replicate exactly what nth_monitor does — pull
#     unread (member_id != self) then filter by can_see — and confirm Carol's
#     relevant set never contains the DM (its preview would otherwise leak).
def monitor_visible(member_id):
    db = srv.get_db()
    try:
        rows = db.execute(
            "SELECT id, mentions, refs, bangs, recipients, member_id, member_name, content "
            "FROM messages WHERE channel IN (?, ?) AND id>0 AND member_id!=? ORDER BY id",
            (CH, AGENT_INBOX_CHANNEL, member_id)).fetchall()
    finally:
        db.close()
    return [m["id"] for m in rows
            if can_see(member_id, "agent", m["member_id"], m["recipients"])]


check("(b) monitor query hides DM from non-recipient", DM_ID not in monitor_visible(carol))
check("(b) monitor query shows DM to recipient", DM_ID in monitor_visible(bob))

# (c) all-seeing is OPERATOR-ONLY. The operator (authenticated loopback/
#     tailscale identity, id _op_l_… / _op_t_…) is all-seeing for audit. A
#     dashboard GUEST (_op_g_…) is a human but NOT the operator and is scoped
#     exactly like an agent — it must NOT read other members' DMs.
OP = "_op_l_host_gabe"        # loopback operator (all-seeing)
GUEST = "_op_g_dave_tok123"   # dashboard guest (human, NOT the operator)
now = srv.now_iso()
db = srv.get_db()
try:
    for mid, nm in ((OP, "Operator"), (GUEST, "Dave-guest")):
        db.execute(
            "INSERT OR IGNORE INTO members (id, channel, name, summary, skills, last_seen, "
            "last_read, joined_at, active, kind) VALUES (?,?,?,?,'',?,0,?,1,'human')",
            (mid, CH, nm, "human", now, now))
        db.execute(
            "INSERT OR IGNORE INTO members (id, channel, name, summary, skills, last_seen, "
            "last_read, joined_at, active, kind) VALUES (?,?,?,?,'',?,0,?,1,'human')",
            (mid, AGENT_INBOX_CHANNEL, nm, "human", now, now))
    db.commit()
finally:
    db.close()

check("(c) operator identity IS all-seeing", is_all_seeing(OP) is True)
check("(c) operator predicate admits any DM (web surface)",
      can_see(OP, "human", alice, json.dumps([bob]), allow_all_seeing=True) is True)
check("(c) GUEST identity is NOT all-seeing", is_all_seeing(GUEST) is False)
check("(c) 'human' kind alone does NOT grant all-seeing (guests scoped)",
      is_all_seeing("agent123", "human") is False)

# (c-guest) a guest does NOT see other members' DMs — even on the all-seeing
# web surface (allow_all_seeing=True), because the guest identity itself is not
# operator. Nor via any agent-facing path.
check("(c-guest) guest does NOT see others' DM via history", DM_ID not in history_ids(GUEST))
check("(c-guest) guest does NOT see others' DM via poll", DM_ID not in poll_ids(GUEST)[0])
check("(c-guest) predicate scopes guest even with allow_all_seeing=True",
      can_see(GUEST, "human", alice, json.dumps([bob]), allow_all_seeing=True) is False)
# ...but the guest DOES see a DM addressed TO the guest, and broadcasts.
gdm = json.loads(srv.nth_dm(channel=CH, member_id=alice, message="hi guest", to=GUEST))
GDM_ID = gdm["message_id"]
check("(c-guest) DM to guest resolved to guest id", gdm.get("recipients") == [GUEST])
check("(c-guest) guest DOES see a DM addressed to it via history", GDM_ID in history_ids(GUEST))

# (c-SECURITY) the agent-facing MCP paths must NOT grant all-seeing from a
# caller-supplied operator id — a forged/real operator id via MCP is scoped
# (allow_all_seeing=False). This is orthogonal to (c): even the real operator
# is not all-seeing over MCP; all-seeing is a WEB (authenticated) property.
check("(c-sec) forged bare _op_ gets NO DMs via history", DM_ID not in history_ids("_op_"))
check("(c-sec) operator id via MCP history is NOT all-seeing (leak closed)",
      DM_ID not in history_ids(OP))
check("(c-sec) operator id via MCP poll is NOT all-seeing (leak closed)",
      DM_ID not in poll_ids(OP)[0])
check("(c-sec) predicate allow_all_seeing=False hides DM from operator id",
      can_see(OP, "human", alice, json.dumps([bob]), allow_all_seeing=False) is False)

# (d) broadcasts still reach everyone (no regression)
bc = json.loads(srv.nth_send(channel=CH, member_id=alice, message="hello everyone"))
BC_ID = bc["message_id"]
check("(d) broadcast reaches recipient-set member Bob", BC_ID in poll_ids(bob)[0])
check("(d) broadcast reaches non-recipient Carol", BC_ID in poll_ids(carol)[0])
check("(d) broadcast in unidentified history", BC_ID in history_ids(""))
check("(d) empty recipients = broadcast in predicate",
      can_see(carol, "agent", alice, "[]") is True
      and can_see(carol, "agent", alice, "") is True
      and can_see(carol, "agent", alice, None) is True)

# (e) the hidden DM advanced Carol's watermark and does not re-surface
#     (Carol polled above, which auto-acks past the raw batch incl. the DM).
wm = watermark(carol)
check("(e) hidden DM advanced non-recipient watermark past it", wm is not None and wm >= DM_ID)
carol_again, _ = poll_ids(carol)
check("(e) hidden DM does not re-surface on next poll", DM_ID not in carol_again)

# ── Dashboard path: operator /api/send with recipients=[bob] is a real DM ──
hub = web.EventHub(srv.DB_PATH, AGENT_INBOX_CHANNEL)
topic_hub = web.EventHub(srv.DB_PATH, CH)
server = None
try:
    hub.start()
    topic_hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    def drain_primed(which_hub, viewer_id, all_seeing):
        qq = which_hub.subscribe(viewer_id=viewer_id, all_seeing=all_seeing)
        got = []
        try:
            while True:
                got.append(json.loads(qq.get_nowait()))
        except Exception:
            pass
        which_hub.unsubscribe(qq)
        return [e["id"] for e in got if e.get("type") == "message"]

    # (c) operator dashboard is all-seeing on the WEB SSE feed: the hub primes
    #     every row (incl. the private DM) to the operator subscriber.
    op_seen = drain_primed(hub, OP, all_seeing=True)
    check("(c) operator SSE feed ships others' DM (all-seeing web surface)", DM_ID in op_seen)

    # (c-guest) a GUEST subscriber's live feed is scoped: others' DMs are
    #     withheld from the primed history; broadcasts and DMs to the guest
    #     still arrive. This is REAL server withholding, not client-side CSS.
    guest_seen = drain_primed(hub, GUEST, all_seeing=False)
    topic_seen = drain_primed(topic_hub, GUEST, all_seeing=False)
    check("(c-guest) guest SSE feed WITHHOLDS others' DM", DM_ID not in guest_seen)
    check("(c-guest) guest SSE feed delivers DM addressed to guest", GDM_ID in guest_seen)
    check("(c-guest) guest SSE feed delivers broadcasts", BC_ID in topic_seen)

    # Direct unit check of the fan-out visibility gate used by prime + live.
    dm_ev = {"type": "message", "id": DM_ID, "member_id": alice, "recipients": [bob]}
    bc_ev = {"type": "message", "id": BC_ID, "member_id": alice, "recipients": []}
    roster_ev = {"type": "roster", "members": []}
    check("(c) _event_visible_to: operator sees DM", web._event_visible_to(dm_ev, OP, True) is True)
    check("(c-guest) _event_visible_to: guest hidden from others' DM",
          web._event_visible_to(dm_ev, GUEST, False) is False)
    check("(c-guest) _event_visible_to: guest sees broadcast",
          web._event_visible_to(bc_ev, GUEST, False) is True)
    check("(c-guest) _event_visible_to: roster always delivered",
          web._event_visible_to(roster_ev, GUEST, False) is True)

    data = json.dumps({"content": "web dm to bob", "recipients": [bob]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/send", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            st, out = resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        st, out = e.code, {}
    if st != 200:
        check("web DM: loopback send accepted", False)
    else:
        WEB_DM = out["id"]
        wr = dm_row(WEB_DM)
        check("web DM: recipients stored", json.loads(wr["recipients"]) == [bob])
        check("web DM: recipient Bob sees it via poll", WEB_DM in poll_ids(bob)[0])
        check("web DM: non-recipient Carol does NOT see it via poll", WEB_DM not in poll_ids(carol)[0])
        # invalid recipients payload → 400
        bad = json.dumps({"content": "x", "recipients": [123]}).encode()
        rq = urllib.request.Request(f"http://127.0.0.1:{port}/api/send", data=bad, method="POST")
        rq.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(rq, timeout=5) as r2:
                bst = r2.status
        except urllib.error.HTTPError as e:
            bst = e.code
        check("web DM: non-string recipient -> 400", bst == 400)
except OSError as e:
    check("web DM: server started", False)
    print(f"  (server error: {e})")
finally:
    if server is not None:
        server.shutdown()
    hub.stop()
    topic_hub.stop()

shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
