"""GET /api/channels — the workspace sidebar's channel list.

The interesting assertions here are the negative ones. A list endpoint is easy
to write so that it "works" while leaking or miscounting:

  * a DM must not raise a channel's unread badge for someone who cannot read
    it, so unread counts only broadcasts;
  * your own messages are not unread to you;
  * the hidden agent inbox is plumbing and must never appear as a room;
  * a guest must not get a cross-channel list at all, previews included, since
    a preview quotes a real message body.

Usage: python tests/test-channels-list.py
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


_tmp = tempfile.mkdtemp(prefix="nth_chlist_")
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


# ── fixture: two real rooms plus the hidden inbox ───────────────────────────
CH_A, alice = connect("Alice", channel="rooma")
CH_B, bob = connect("Bob", channel="roomb")
srv.nth_send(channel=CH_A, member_id=alice, message="first in A")
srv.nth_send(channel=CH_A, member_id=alice, message="second in A")
srv.nth_send(channel=CH_B, member_id=bob, message="only in B")

hub = web.EventHub(srv.DB_PATH, CH_A)
server = None
try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH_A
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
            "SELECT id, name FROM members "
            "WHERE channel=? AND id LIKE '\\_op\\_%' ESCAPE '\\'",
            (CH_A,)).fetchone()
        operator_id, operator_name = oprow[0], oprow[1]
    finally:
        db.close()

    st, body = http(port, "/api/channels")
    check("list: answers 200", st == 200 and body.get("ok") is True)
    by_code = {c["code"]: c for c in body.get("channels", [])}
    check("list: both real rooms are present", {CH_A, CH_B} <= set(by_code))
    check("list: the hidden agent inbox is NOT listed as a room",
          web.AGENT_INBOX_CHANNEL not in by_code)
    check("list: reports the member count", by_code[CH_A]["members"] >= 2)
    check("list: carries a preview of the latest message",
          "operator here" in by_code[CH_A]["preview"])
    check("list: preview names the author",
          by_code[CH_B]["preview"].startswith("Bob:"))
    check("list: ordered by most recent activity first",
          [c["code"] for c in body["channels"]][0] == CH_A)

    # ── unread accounting ───────────────────────────────────────────────────
    # Room B holds Bob's "[joined]" line and his one message; a join notice is
    # an ordinary broadcast row and does count, matching the shipped behaviour.
    check("unread: a peer's messages start unread", by_code[CH_B]["unread"] == 2)
    # Room A holds Alice's join + her two, plus the operator's own send, which
    # must NOT be counted against the operator.
    check("unread: your OWN message is not unread to you",
          by_code[CH_A]["unread"] == 3)

    ids = [r["id"] for r in json.loads(
        srv.nth_history(channel=CH_A, last_n=50))["messages"]]
    http(port, "/api/messages/mark-read", "POST", {"ids": ids})
    _st, body2 = http(port, "/api/channels")
    by2 = {c["code"]: c for c in body2["channels"]}
    check("unread: marking them read clears the count", by2[CH_A]["unread"] == 0)
    check("unread: and does not touch the other room", by2[CH_B]["unread"] == 2)

    # ── an addressed message must not raise the channel's unread badge ──────
    # This is what separates a correct list from a leaky one. A row carrying
    # `recipients` is addressed to specific members, not broadcast to the room,
    # so it must not badge the channel for someone who is not a recipient.
    #
    # Written directly rather than through trio_dm, which routes over the
    # hidden inbox channel that this query already excludes wholesale — that
    # path would pass even with the recipients guard deleted, and prove
    # nothing about it.
    _c, carol = connect("Carol", channel=CH_B)
    before_dm = None
    _st, pre = http(port, "/api/channels")
    before_dm = {c["code"]: c for c in pre["channels"]}[CH_B]["unread"]
    db = srv.get_db()
    try:
        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, "
            "recipients, created_at) VALUES (?,?,?,?,?,?)",
            (CH_B, bob, "Bob", "private to carol",
             json.dumps([carol]), srv.now_iso()))
        db.commit()
    finally:
        db.close()
    _st, body3 = http(port, "/api/channels")
    by3 = {c["code"]: c for c in body3["channels"]}
    check("unread: an addressed (recipients-scoped) message does NOT raise "
          "the operator's unread count", by3[CH_B]["unread"] == before_dm)

    # ── mentions are a subset of unread ─────────────────────────────────────
    srv.nth_send(channel=CH_A, member_id=alice,
                 message=f"hey @{operator_name} look at this")
    _st, body4 = http(port, "/api/channels")
    by4 = {c["code"]: c for c in body4["channels"]}
    check("mentions: a message naming the operator counts as a mention",
          by4[CH_A]["unread_mentions"] == 1)
    check("mentions: and also counts as plain unread",
          by4[CH_A]["unread"] == 1)
    check("mentions: a channel with no mention of you reports zero",
          by4[CH_B]["unread_mentions"] == 0)

    # The probe searches the mentions JSON for the QUOTED id, so an id that
    # merely CONTAINS the operator's id is not a mention of the operator.
    # Without the quotes this is a false positive, and operator ids carry '_'
    # and shared prefixes, which is exactly when that bites.
    mentions_before = by4[CH_A]["unread_mentions"]
    db = srv.get_db()
    try:
        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, "
            "mentions, created_at) VALUES (?,?,?,?,?,?)",
            (CH_A, alice, "Alice", "not actually for the operator",
             json.dumps([operator_id + "_lookalike"]), srv.now_iso()))
        db.commit()
    finally:
        db.close()
    _st, body5 = http(port, "/api/channels")
    by5 = {c["code"]: c for c in body5["channels"]}
    check("mentions: an id that merely CONTAINS the operator's id is not a "
          "mention of the operator", by5[CH_A]["unread_mentions"] == mentions_before)

    # ── archived channels are a separate view ───────────────────────────────
    db = srv.get_db()
    try:
        db.execute("UPDATE channels SET archived_at = ? WHERE code = ?",
                   (srv.now_iso(), CH_B))
        db.commit()
    finally:
        db.close()
    _st, live = http(port, "/api/channels")
    check("archive: an archived channel drops out of the default list",
          CH_B not in {c["code"] for c in live["channels"]})
    _st, arch = http(port, "/api/channels?archived=1")
    check("archive: and appears under archived=1",
          CH_B in {c["code"] for c in arch["channels"]})
    check("archive: the archived view excludes live channels",
          CH_A not in {c["code"] for c in arch["channels"]})
    check("archive: archived rows carry their archived_at",
          all(c["archived_at"] for c in arch["channels"]))

    # ── the operator gate, asserted behaviourally ───────────────────────────
    _real = web.NthWebHandler._resolve_identity
    try:
        class _Guest:
            source = web.IDENTITY_SOURCE_GUEST
            name = "guest"
            summary = "guest"
            member_id = "guest_intruder"
        web.NthWebHandler._resolve_identity = lambda self: (None, _Guest(), False)
        st_g, body_g = http(port, "/api/channels")
        check("authz: a guest is refused the cross-channel list", st_g == 403)
        check("authz: and no channel previews leak in the refusal body",
              "only in B" not in json.dumps(body_g))
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
print("OK — the sidebar list counts broadcasts only, hides the inbox, "
      "separates archives, and refuses guests")
