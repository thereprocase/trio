"""GET /api/dms and POST /api/archives — unified DM threading and archiving.

A DM's identity is its PARTICIPANTS, not its channel. The same two people may
have rows sitting in several backing channels (older DMs are scattered across
whatever topic they were sent in, new ones go through the global agent inbox),
and to a reader that is ONE conversation. So the assertions that matter are:

  * two rows in different channels with the same participants merge into one
    thread, and `?with=` returns both;
  * a conversation the operator is not part of is an AUDIT thread, listed
    separately and never mixed into their own;
  * archiving stores a watermark, so a NEWER message un-archives the thread by
    itself — the property a boolean flag cannot have;
  * archiving an AGENT archives your threads with it, derived rather than
    stored, so unarchiving the agent restores them.

Usage: python tests/test-dm-threads.py
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
import nth_conversation as nconv  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_dms_")
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


def dm_row(channel, sender, sender_name, recipients, content):
    """Write a DM row directly, so a thread can be spread across channels."""
    db = srv.get_db()
    try:
        cur = db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, "
            "recipients, created_at) VALUES (?,?,?,?,?,?)",
            (channel, sender, sender_name, content,
             json.dumps(recipients), srv.now_iso()))
        db.commit()
        return cur.lastrowid
    finally:
        db.close()


CH_A, alice = connect("Alice", channel="dmroom")
CH_B, bob = connect("Bob", channel="otherroom")
_c, carol = connect("Carol", channel=CH_A)

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
        op = db.execute(
            "SELECT id FROM members WHERE channel=? "
            "AND id LIKE '\\_op\\_%' ESCAPE '\\'", (CH_A,)).fetchone()[0]
    finally:
        db.close()

    # Alice DMs the operator from TWO different channels. One conversation.
    m1 = dm_row(CH_A, alice, "Alice", [op], "first, from dmroom")
    m2 = dm_row(CH_B, alice, "Alice", [op], "second, from otherroom")
    # A group DM with Alice and Carol — must not collapse into the 1:1.
    dm_row(CH_A, alice, "Alice", [op, carol], "group hello")
    # Bob and Carol talk to each other; the operator is not involved.
    dm_row(CH_B, bob, "Bob", [carol], "agent to agent")

    # ── /api/meta must not require a channel ──────────────────────────────
    # The workspace boots at "/" with NO channel selected and calls /api/meta to
    # learn who it is. This used to 400 in landing mode, so boot() set
    # state.operator = null for the whole session and every "am I a party to
    # this?" check answered no. It surfaced as DMs that listed but rendered
    # empty — neither side's messages — which reads as an agent that never
    # replied rather than as a broken identity.
    #
    # Identity is global: _resolve_identity() never consults a channel, and the
    # channel in the response is only echoed back.
    _keep_landing, _keep_channel = web.NthWebHandler.landing_mode, web.NthWebHandler.channel
    web.NthWebHandler.landing_mode, web.NthWebHandler.channel = True, None
    try:
        st_m, meta = http(port, "/api/meta")
        check("meta: 200 with no channel param (landing mode)", st_m == 200)
        check("meta: still identifies the operator without a channel",
              bool((meta.get("operator") or {}).get("id")))
        check("meta: channel is empty rather than absent",
              meta.get("channel") == "")
        st_c, meta_c = http(port, "/api/meta?channel=" + CH_A)
        check("meta: a named channel is still echoed back",
              st_c == 200 and meta_c.get("channel") == CH_A)
        check("meta: same operator either way",
              (meta.get("operator") or {}).get("id")
              == (meta_c.get("operator") or {}).get("id"))
    finally:
        web.NthWebHandler.landing_mode, web.NthWebHandler.channel = _keep_landing, _keep_channel

    st, body = http(port, "/api/dms")
    check("dms: answers 200", st == 200 and body.get("ok") is True)
    mine = {t["key"]: t for t in body["your_dms"]}
    audit = {t["key"]: t for t in body["agent_dms"]}

    k_alice = nconv.canonical_dm_key([alice, op])
    k_group = nconv.canonical_dm_key([alice, carol, op])
    check("threading: a 1:1 thread is keyed by its participant set",
          k_alice in mine)
    check("threading: rows in DIFFERENT channels merge into ONE thread",
          len([k for k in mine if k == k_alice]) == 1)
    check("threading: the merged thread's latest is the newest row, "
          "whichever channel it came from", mine[k_alice]["last_id"] == m2)
    check("threading: a group DM is a SEPARATE thread, not folded into the 1:1",
          k_group in mine and k_group != k_alice)
    check("threading: the group key names every participant",
          set(nconv.participants_in_key(k_group)) == {alice, carol, op})
    # The property the canonical key exists for: the SAME conversation has the
    # SAME name whoever is asking, so a thread link can be shared and a search
    # hit can be attributed to a thread.
    check("threading: the key is viewer-INDEPENDENT — it does not change with "
          "who is looking",
          nconv.canonical_dm_key([alice, op])
          == nconv.canonical_dm_key([op, alice]))
    check("threading: and it names the whole participant set, not 'the other "
          "person'", op in nconv.participants_in_key(k_alice))

    check("audit: a conversation the operator is not in is listed separately",
          len(audit) == 1)
    check("audit: and is NOT mixed into the operator's own threads",
          not any(bob in k and carol in k for k in mine))

    check("unread: peer DMs start unread", mine[k_alice]["unread"] == 2)
    http(port, "/api/messages/mark-read", "POST", {"ids": [m1]})
    _st, b2 = http(port, "/api/dms")
    check("unread: marking one read decrements the thread",
          {t["key"]: t for t in b2["your_dms"]}[k_alice]["unread"] == 1)

    # ── ?with= returns the merged history ───────────────────────────────────
    _st, thread = http(port, f"/api/dms?with={k_alice}")
    contents = [m["content"] for m in thread["messages"]]
    check("with: returns this thread's messages oldest-first",
          contents == ["first, from dmroom", "second, from otherroom"])
    check("with: each message carries the channel it actually lives in",
          {m["channel"] for m in thread["messages"]} == {CH_A, CH_B})
    check("with: an unrelated thread's messages are not included",
          "group hello" not in contents and "agent to agent" not in contents)

    # ── archiving a DM thread ───────────────────────────────────────────────
    st_a, _ = http(port, "/api/archives", "POST",
                   {"kind": "dm", "key": k_alice, "archived": True})
    check("archive: archiving a DM thread succeeds", st_a == 200)
    _st, b3 = http(port, "/api/dms")
    check("archive: it leaves the active list",
          k_alice not in {t["key"] for t in b3["your_dms"]})
    _st, b4 = http(port, "/api/dms?archived=1")
    check("archive: and appears in the archived list",
          k_alice in {t["key"] for t in b4["your_dms"]})
    _st, arch_thread = http(port, f"/api/dms?with={k_alice}&archived=1")
    check("archive: its history is still readable from the archive view",
          len(arch_thread["messages"]) == 2)

    # The watermark property: a NEWER message revives the thread with no
    # explicit un-archive. A boolean flag could not do this.
    dm_row(CH_A, alice, "Alice", [op], "are you there?")
    _st, b5 = http(port, "/api/dms")
    check("archive: a NEWER message un-archives the thread by itself — "
          "the reason the marker is a watermark, not a flag",
          k_alice in {t["key"] for t in b5["your_dms"]})

    st_r, _ = http(port, "/api/archives", "POST",
                   {"kind": "dm", "key": k_alice, "archived": False})
    check("archive: explicit restore succeeds", st_r == 200)

    # ── archiving the AGENT archives your threads with it ───────────────────
    db = srv.get_db()
    try:
        # A self-connected agent already has an agents row (identity minting),
        # so stamp the archive rather than inserting a duplicate.
        db.execute(
            "INSERT OR IGNORE INTO agents (id, name, model, base_prompt, "
            "state, managed, created_at, last_active_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (alice, "Alice", "m", "", "stopped", 1,
             srv.now_iso(), srv.now_iso()))
        db.execute("UPDATE agents SET archived_at=? WHERE id=?",
                   (srv.now_iso(), alice))
        db.commit()
    finally:
        db.close()
    _st, b6 = http(port, "/api/dms")
    check("agent archive: archiving the AGENT hides your thread with it",
          k_alice not in {t["key"] for t in b6["your_dms"]})
    _st, b7 = http(port, "/api/dms?archived=1")
    got = {t["key"]: t for t in b7["your_dms"]}
    check("agent archive: the thread shows up as archived", k_alice in got)
    check("agent archive: flagged as agent_archived so the client can say "
          "restoring means unarchiving the agent",
          got.get(k_alice, {}).get("agent_archived") is True)
    check("agent archive: a GROUP thread with one live peer stays active",
          k_group in {t["key"] for t in b6["your_dms"]})

    # ── channel archive ─────────────────────────────────────────────────────
    st_c, _ = http(port, "/api/archives", "POST",
                   {"kind": "channel", "key": CH_B, "archived": True})
    check("channel archive: succeeds", st_c == 200)
    _st, chans = http(port, "/api/channels")
    check("channel archive: drops out of the active channel list",
          CH_B not in {c["code"] for c in chans["channels"]})
    st_cr, _ = http(port, "/api/archives", "POST",
                    {"kind": "channel", "key": CH_B, "archived": False})
    _st, chans2 = http(port, "/api/channels")
    check("channel archive: restore brings it back",
          st_cr == 200 and CH_B in {c["code"] for c in chans2["channels"]})

    # ── validation and boundaries ───────────────────────────────────────────
    st_v, _ = http(port, "/api/archives", "POST",
                   {"kind": "nonsense", "key": "x", "archived": True})
    check("validation: kind must be channel or dm", st_v == 400)
    st_v2, _ = http(port, "/api/archives", "POST",
                    {"kind": "dm", "key": k_alice, "archived": "yes"})
    check("validation: archived must be a boolean", st_v2 == 400)
    st_v3, _ = http(port, "/api/archives", "POST",
                    {"kind": "channel", "key": "no-such-channel",
                     "archived": True})
    check("validation: an unknown channel is a 404", st_v3 == 404)
    st_v4, _ = http(port, "/api/archives", "POST",
                    {"kind": "channel", "key": web.AGENT_INBOX_CHANNEL,
                     "archived": True})
    check("boundary: the hidden agent inbox cannot be archived", st_v4 == 400)
    # The audit thread belongs to Bob and Carol; the operator is not in it, so
    # dm_thread_key yields "" for those rows and can never match its key.
    audit_key = next(iter(audit))
    st_v5, _ = http(port, "/api/archives", "POST",
                    {"kind": "dm", "key": audit_key, "archived": True})
    check("boundary: you cannot archive a DM thread you are not part of",
          st_v5 == 404)

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
        st_g, body_g = http(port, "/api/dms")
        check("authz: a guest cannot read the DM surface", st_g == 403)
        check("authz: and no DM content leaks in the refusal",
              "first, from dmroom" not in json.dumps(body_g))
        st_ga, _ = http(port, "/api/archives", "POST",
                        {"kind": "dm", "key": k_alice, "archived": True})
        check("authz: a guest cannot archive", st_ga == 403)
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
print("OK — DM threads merge by participant, audit threads stay separate, "
      "and archives are watermarks that new messages revive")
