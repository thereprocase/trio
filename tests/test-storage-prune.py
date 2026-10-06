"""GET /api/storage, POST /api/prune, /avatars/, size estimates, workspace SSE.

Prune is the only DESTRUCTIVE endpoint on this branch, so most of this file is
about what it refuses and what it leaves alone:

  * dry_run defaults to TRUE, so a body that forgets the key previews instead
    of deleting — the single most valuable property here;
  * an ACTIVE channel is never touched by prune_archived_messages;
  * the agent inbox can never be deleted;
  * a guest, and even a plain operator who is not from a trusted source, are
    refused before the body is parsed;
  * message_reads rows are reaped with their messages — the FK is declared but
    never enforced, so nothing else would collect them.

Usage: python tests/test-storage-prune.py
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


_tmp = tempfile.mkdtemp(prefix="nth_prune_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def connect(name, channel=""):
    r = json.loads(srv.nth_connect(summary="t", name=name, channel=channel))
    return r["channel"], r["member_id"]


def http(port, path, method="GET", body=None, raw=False):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, (payload if raw
                                 else json.loads(payload.decode()))
    except urllib.error.HTTPError as e:
        try:
            return e.code, (e.read() if raw else json.loads(e.read().decode()))
        except Exception:                                   # noqa: BLE001
            return e.code, {}


def counts():
    db = srv.get_db()
    try:
        return (db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0])
    finally:
        db.close()


CH, alice = connect("Alice", channel="keepme")
OLD, bob = connect("Bob", channel="oldroom")
for i in range(4):
    srv.nth_send(channel=CH, member_id=alice, message=f"live message {i}")
for i in range(3):
    srv.nth_send(channel=OLD, member_id=bob, message=f"stale message {i}")

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

    # ── avatars ─────────────────────────────────────────────────────────────
    name = web._CHARACTERS[0][1]
    st_a, payload = http(port, f"/avatars/{name}/avatar.svg", raw=True)
    check("avatars: a known character SVG is served", st_a == 200)
    check("avatars: with an SVG body", payload.lstrip()[:5] in (b"<svg ", b"<?xml"))
    st_b, _ = http(port, "/avatars/NotACharacter/avatar.svg", raw=True)
    check("avatars: an unknown name is a 404", st_b == 404)
    # The allowlist is the defence, so test it against a file that EXISTS on
    # disk but is not a known character. Asking for a name that is simply
    # absent proves nothing — that 404s from the missing file alone, with or
    # without the check.
    sneaky = SERVER / "web" / "avatars" / "Sneaky"
    sneaky.mkdir(parents=True, exist_ok=True)
    (sneaky / "avatar.svg").write_text("<svg>not a real character</svg>")
    try:
        st_c, body_c = http(port, "/avatars/Sneaky/avatar.svg", raw=True)
        check("avatars: a file on disk that is NOT in the character allowlist "
              "is refused", st_c == 404)
        check("avatars: and its contents are not served",
              b"not a real character" not in (body_c or b""))
    finally:
        shutil.rmtree(sneaky, ignore_errors=True)
    check("avatars: every character in the roster has an asset on disk",
          all((SERVER / "web" / "avatars" / a / "avatar.svg").exists()
              for _n, a in web._CHARACTERS))
    check("avatars: the Character pool matches the product list", web._CHARACTERS == [
        ("Luna", "Luna"), ("Iris", "Iris"), ("Gale", "Gale"),
        ("Frost", "Frost"), ("Umbra", "Umbra"), ("Atlas", "Atlas"),
        ("Chance", "Chance"), ("Gemma", "Gemma"), ("Rex", "Rex"),
        ("Locke", "Locke"), ("Corbin", "Corbin"), ("Vesper", "Vesper"),
        ("Salem", "Salem"), ("Merlin", "Merlin"), ("Circe", "Circe"),
        ("Piper", "Piper"), ("Reed", "Reed"), ("Coda", "Coda"),
        ("Cass", "Cass"), ("Quill", "Quill"), ("Scout", "Scout"),
        ("Paige", "Paige"), ("Darwin", "Darwin"), ("Ada", "Ada"),
        ("Watts", "Watts"), ("Ferris", "Ferris"), ("Mason", "Mason"),
        ("Grove", "Grove"), ("Archer", "Archer"), ("Ranger", "Ranger"),
    ])

    # ── storage ─────────────────────────────────────────────────────────────
    st_s, sb = http(port, "/api/storage")
    check("storage: answers 200", st_s == 200 and sb.get("ok") is True)
    check("storage: reports a positive db size", sb["db_bytes"] > 0)
    by = {c["channel"]: c for c in sb["by_channel"]}
    check("storage: breaks down by channel", CH in by and OLD in by)
    check("storage: counts messages per channel", by[CH]["message_count"] >= 4)
    check("storage: estimates message bytes", by[CH]["est_message_bytes"] > 0)
    check("storage: reports reclaimable bytes separately",
          "db_reclaimable_bytes" in sb)

    # ── size estimates ──────────────────────────────────────────────────────
    st_z, zb = http(port, "/api/channel-size")
    check("size: answers 200 with a token estimate",
          st_z == 200 and zb["estimated_tokens"] > 0)
    check("size: counts this channel's messages", zb["message_count"] >= 4)

    # ── workspace SSE ───────────────────────────────────────────────────────
    def read_stream(url, out):
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                out.append(r.status)
                out.append(r.headers.get("Content-Type", ""))
                r.read(1)
        except Exception as exc:                            # noqa: BLE001
            out.append(repr(exc))

    got = []
    t = threading.Thread(target=read_stream,
                         args=(f"http://127.0.0.1:{port}/api/workspace/events",
                               got), daemon=True)
    t.start()
    time.sleep(0.4)
    srv.nth_send(channel=CH, member_id=alice, message="live one")
    t.join(timeout=6)
    check("workspace sse: opens with an event-stream content type",
          got[:1] == [200] and "text/event-stream" in (got[1] if len(got) > 1 else ""))

    # ── prune: dry-run is the default ───────────────────────────────────────
    before_msgs, _ = counts()
    st_p, pb = http(port, "/api/prune", "POST",
                    {"action": "prune_archived_messages",
                     "older_than_days": 0})
    check("prune: a body with no dry_run key is treated as a DRY RUN",
          st_p == 200 and pb.get("dry_run") is True)
    check("prune: the dry run deleted nothing", counts()[0] == before_msgs)
    check("prune: a dry run reports what it WOULD free, not what it freed",
          "would_free_bytes" in pb and "freed_bytes" not in pb)

    # ── prune never touches an ACTIVE channel ───────────────────────────────
    st_r, rb = http(port, "/api/prune", "POST",
                    {"action": "prune_archived_messages",
                     "older_than_days": 0, "dry_run": False})
    check("prune: a real run succeeds", st_r == 200 and rb.get("ok") is True)
    check("prune: with no archived channels, nothing is deleted",
          counts()[0] == before_msgs)

    # Archive one channel, then prune: only that channel loses messages.
    http(port, "/api/archives", "POST",
         {"kind": "channel", "key": OLD, "archived": True})
    db = srv.get_db()
    try:
        old_ids = [r[0] for r in db.execute(
            "SELECT id FROM messages WHERE channel=?", (OLD,)).fetchall()]
        live_before = db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel=?", (CH,)).fetchone()[0]
    finally:
        db.close()
    # Give those messages read receipts, to prove they are reaped with them.
    http(port, "/api/messages/mark-read", "POST", {"ids": old_ids})
    check("fixture: the archived channel's messages have read receipts",
          counts()[1] > 0)

    st_d, dbody = http(port, "/api/prune", "POST",
                       {"action": "prune_archived_messages",
                        "older_than_days": 0, "dry_run": False})
    db = srv.get_db()
    try:
        old_left = db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel=?", (OLD,)).fetchone()[0]
        live_left = db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel=?", (CH,)).fetchone()[0]
        reads_left = db.execute(
            "SELECT COUNT(*) FROM message_reads WHERE message_id IN "
            f"({','.join('?' * len(old_ids))})", old_ids).fetchone()[0]
    finally:
        db.close()
    check("prune: the ARCHIVED channel's messages are deleted",
          st_d == 200 and old_left == 0)
    check("prune: the ACTIVE channel is untouched", live_left == live_before)
    check("prune: message_reads rows are reaped with their messages — the FK "
          "is declared but never enforced, so nothing else collects them",
          reads_left == 0)
    check("prune: a real run reports freed bytes", "freed_bytes" in dbody)

    # ── delete_channel ──────────────────────────────────────────────────────
    st_dc, _ = http(port, "/api/prune", "POST",
                    {"action": "delete_channel", "channel": OLD,
                     "dry_run": False})
    db = srv.get_db()
    try:
        gone = db.execute("SELECT 1 FROM channels WHERE code=?",
                          (OLD,)).fetchone()
        members_left = db.execute(
            "SELECT COUNT(*) FROM members WHERE channel=?", (OLD,)).fetchone()[0]
    finally:
        db.close()
    check("prune: delete_channel removes the channel row",
          st_dc == 200 and gone is None)
    check("prune: and its membership", members_left == 0)

    # ── refusals ────────────────────────────────────────────────────────────
    st_i, _ = http(port, "/api/prune", "POST",
                   {"action": "delete_channel",
                    "channel": web.AGENT_INBOX_CHANNEL, "dry_run": False})
    check("refuse: the agent inbox can never be deleted", st_i == 400)
    st_u, _ = http(port, "/api/prune", "POST",
                   {"action": "drop_everything", "dry_run": False})
    check("refuse: an unknown action is a 400", st_u == 400)
    st_n, _ = http(port, "/api/prune", "POST",
                   {"action": "prune_attachments", "older_than_days": -1})
    check("refuse: a negative age is a 400", st_n == 400)
    st_bo, _ = http(port, "/api/prune", "POST",
                    {"action": "prune_attachments", "older_than_days": True})
    check("refuse: a boolean age is a 400 — bool subclasses int", st_bo == 400)
    st_dr, _ = http(port, "/api/prune", "POST",
                    {"action": "reclaim", "dry_run": "no"})
    check("refuse: a non-boolean dry_run is a 400", st_dr == 400)

    # ── the trusted-source gate ─────────────────────────────────────────────
    # An all-seeing operator is not enough: storage sizes expose every channel
    # and prune destroys data, so a self-declared guest must be refused even
    # though loopback would normally make it all-seeing.
    _real = web.NthWebHandler._resolve_identity
    try:
        class _Guest:
            source = web.IDENTITY_SOURCE_GUEST
            name = "guest"
            summary = "guest"
            member_id = "guest_intruder"
            display_name = "guest"
        web.NthWebHandler._resolve_identity = lambda self: (None, _Guest(), False)
        before_all = counts()[0]
        st_gs, _ = http(port, "/api/storage")
        st_gp, _ = http(port, "/api/prune", "POST",
                        {"action": "delete_channel", "channel": CH,
                         "dry_run": False})
        check("authz: a guest cannot read storage", st_gs == 403)
        check("authz: a guest cannot prune", st_gp == 403)
        check("authz: and the refused prune destroyed nothing",
              counts()[0] == before_all)
    finally:
        web.NthWebHandler._resolve_identity = _real

    # An identity that is all-seeing but self-declared must still be refused.
    #
    # NOTE ON WHAT THIS DOES AND DOESN'T PROVE. Today the refusal comes from
    # the id PREFIX: is_all_seeing() admits only `_op_l_`/`_op_t_`, so the
    # explicit source check in _require_trusted_operator is redundant and
    # deleting it leaves these assertions green (verified by mutation). This
    # pins the CONTRACT — all-seeing alone is not sufficient — not the
    # particular line that currently enforces it.
    _real = web.NthWebHandler._resolve_identity
    try:
        db = srv.get_db()
        try:
            op_id = db.execute(
                "SELECT id FROM members WHERE channel=? "
                "AND id LIKE '\\_op\\_%' ESCAPE '\\'", (CH,)).fetchone()[0]
        finally:
            db.close()
        check("fixture: the operator id really is all-seeing",
              web.is_all_seeing(op_id))

        class _UntrustedOperator:
            source = web.IDENTITY_SOURCE_GUEST
            name = "self-declared"
            summary = "self-declared"
            member_id = op_id
            display_name = "self-declared"
        web.NthWebHandler._resolve_identity = (
            lambda self: (None, _UntrustedOperator(), False))
        before_untrusted = counts()[0]
        st_us, _ = http(port, "/api/storage")
        st_up, _ = http(port, "/api/prune", "POST",
                        {"action": "delete_channel", "channel": CH,
                         "dry_run": False})
        check("authz: an ALL-SEEING but self-declared identity cannot read "
              "storage — all-seeing alone is not enough", st_us == 403)
        check("authz: nor prune", st_up == 403)
        check("authz: and that refusal destroyed nothing",
              counts()[0] == before_untrusted)
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
print("OK — prune previews by default, spares active channels, reaps read "
      "receipts, and refuses anyone but a trusted operator")
