"""Per-reader read state: the substrate the workspace sidebar counts against.

/api/messages/mark-read records WHICH messages a reader has seen, as a set.
That is deliberately not members.last_read, which is a single high-water mark
and cannot answer the sidebar's question once the operator reads out of order.

The tests that matter here are the ones about whose state is being written:
`member_id` comes from the resolved identity, never from the request body, so
a caller cannot mark messages read on someone else's behalf. A live loopback
probe resolves to an all-seeing operator, so the refusal path is exercised by
forcing a guest identity and asserting the HTTP response — a policy-data check
could not catch the gate being deleted.

Usage: python tests/test-message-reads.py
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


_tmp = tempfile.mkdtemp(prefix="nth_reads_")
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


def reads_for(member_id):
    db = srv.get_db()
    try:
        return {r[0] for r in db.execute(
            "SELECT message_id FROM message_reads WHERE member_id = ?",
            (member_id,)).fetchall()}
    finally:
        db.close()


# ── the table exists at all ─────────────────────────────────────────────────
CH, peer = connect("Peer", channel="readstest")
db = srv.get_db()
try:
    cols = {r[1] for r in db.execute("PRAGMA table_info(message_reads)").fetchall()}
    check("schema: message_reads exists with the expected columns",
          cols == {"message_id", "member_id", "read_at"})
    idx = {r[1] for r in db.execute("PRAGMA index_list(message_reads)").fetchall()}
    check("schema: the per-member lookup index exists",
          any("member" in name for name in idx))
finally:
    db.close()

mids = []
for i in range(5):
    r = json.loads(srv.nth_send(channel=CH, member_id=peer, message=f"m{i}"))
    mids.append(r["message_id"])

hub = web.EventHub(srv.DB_PATH, CH)
server = None
operator_id = None
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

    # First send materialises the loopback operator row (id prefixed _op_).
    http(port, "/api/send", "POST", {"content": "hello"})
    db = srv.get_db()
    try:
        row = db.execute(
            "SELECT id FROM members WHERE channel=? AND id LIKE '\\_op\\_%' ESCAPE '\\'",
            (CH,)).fetchone()
        operator_id = row[0] if row else None
    finally:
        db.close()
    check("fixture: the loopback operator has a member row", operator_id is not None)

    # ── marking read ────────────────────────────────────────────────────────
    st, body = http(port, "/api/messages/mark-read", "POST",
                    {"ids": [mids[0], mids[1]]})
    check("mark-read: accepted", st == 200 and body.get("ok") is True)
    check("mark-read: reports how many ids it accepted", body.get("updated") == 2)
    check("mark-read: wrote exactly those two, for the operator",
          reads_for(operator_id) == {mids[0], mids[1]})

    # Out-of-order is the whole reason this is a set and not a watermark.
    http(port, "/api/messages/mark-read", "POST", {"ids": [mids[4]]})
    check("mark-read: reading the NEWEST does not mark the older ones read — "
          "the failure a last_read watermark cannot avoid",
          reads_for(operator_id) == {mids[0], mids[1], mids[4]})

    # ── idempotence in both directions ──────────────────────────────────────
    st_r, body_r = http(port, "/api/messages/mark-read", "POST",
                        {"ids": [mids[0], mids[1]]})
    check("mark-read: re-marking the same ids still reports success",
          st_r == 200 and body_r.get("updated") == 2)
    check("mark-read: and does not duplicate rows",
          reads_for(operator_id) == {mids[0], mids[1], mids[4]})

    st_u, _ = http(port, "/api/messages/mark-read", "POST",
                   {"ids": [mids[0]], "read": False})
    check("mark-read: read=false clears one back to unread",
          st_u == 200 and reads_for(operator_id) == {mids[1], mids[4]})
    st_u2, _ = http(port, "/api/messages/mark-read", "POST",
                    {"ids": [mids[0]], "read": False})
    check("mark-read: clearing an already-unread id is not an error",
          st_u2 == 200)

    st_e, body_e = http(port, "/api/messages/mark-read", "POST", {"ids": []})
    check("mark-read: an empty batch is a no-op, not an error",
          st_e == 200 and body_e.get("updated") == 0)

    # ── input validation ────────────────────────────────────────────────────
    st_b, _ = http(port, "/api/messages/mark-read", "POST", {"ids": "nope"})
    check("validation: ids must be a list", st_b == 400)
    st_b2, _ = http(port, "/api/messages/mark-read", "POST", {"ids": ["1"]})
    check("validation: ids must be integers, not strings", st_b2 == 400)
    # bool is a subclass of int; without an explicit guard [True] would be
    # accepted and written as message_id=1, silently marking a real message.
    before = reads_for(operator_id)
    st_b3, _ = http(port, "/api/messages/mark-read", "POST", {"ids": [True]})
    check("validation: a boolean is rejected, not written as message_id=1",
          st_b3 == 400 and reads_for(operator_id) == before)
    st_b4, _ = http(port, "/api/messages/mark-read", "POST",
                    {"ids": [mids[0]], "read": "yes"})
    check("validation: read must be a boolean", st_b4 == 400)
    st_b5, _ = http(port, "/api/messages/mark-read", "POST",
                    {"ids": list(range(1001))})
    check("validation: an oversized batch is refused", st_b5 == 400)

    # ── the privacy boundary ────────────────────────────────────────────────
    # member_id is taken from the identity, never the body. A caller supplying
    # someone else's id must not be able to write THEIR read state.
    http(port, "/api/messages/mark-read", "POST",
         {"ids": [mids[2]], "member_id": peer})
    check("privacy: a member_id in the body is ignored — the peer's read "
          "state is untouched", reads_for(peer) == set())
    check("privacy: the row landed on the caller instead",
          mids[2] in reads_for(operator_id))

    # ── the operator gate, asserted behaviourally ───────────────────────────
    # A loopback probe IS an all-seeing operator, so the refusal path is
    # unreachable without forcing a guest identity.
    _real = web.NthWebHandler._resolve_identity
    try:
        class _Guest:
            source = web.IDENTITY_SOURCE_GUEST
            name = "guest"
            summary = "guest"
            member_id = "guest_intruder"
        web.NthWebHandler._resolve_identity = lambda self: (None, _Guest(), False)
        st_g, _ = http(port, "/api/messages/mark-read", "POST", {"ids": [mids[3]]})
        check("authz: a guest is refused", st_g in (401, 403))
        check("authz: and the refused write left no row behind",
              reads_for("guest_intruder") == set())
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
print("OK — read state is per-reader, set-valued, idempotent, and operator-only")
