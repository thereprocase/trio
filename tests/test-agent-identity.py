"""A self-connected agent gets a durable, reclaimable global identity.

`members` is channel-scoped, so before this an agent that connected itself had
no row in the global `agents` registry and therefore nothing to reclaim: after
a restart it came back as a SECOND member, and every @mention, placement and
heartbeat still pointed at the identity it had abandoned. Only
supervisor-spawned agents could reclaim.

The security shape is the point of most of these checks. `reclaim_secret` is
the entire credential for speaking as an identity, and `member_id` is public —
it is on the roster every peer can read. So: the secret is disclosed exactly
once, at mint; a reclaim never echoes it back; an unknown id is never honoured;
and a human's row is never reclaimable at all.

Usage: python tests/test-agent-identity.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

_tmp = Path(tempfile.mkdtemp(prefix="nth_identity_"))
os.environ["NTH_HOME"] = str(_tmp)

import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


srv.DB_DIR = _tmp
srv.DB_PATH = _tmp / "nth.db"


def connect(**kw):
    return json.loads(srv.nth_connect(**kw))


def db():
    conn = sqlite3.connect(str(srv.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def agent_row(agent_id):
    conn = db()
    try:
        return conn.execute("SELECT * FROM agents WHERE id = ?",
                            (agent_id,)).fetchone()
    finally:
        conn.close()


def joins(channel):
    conn = db()
    try:
        return [r[0] for r in conn.execute(
            "SELECT content FROM messages WHERE channel = ? "
            "AND content LIKE '[joined]%' ORDER BY id", (channel,)).fetchall()]
    finally:
        conn.close()


try:
    # ── minting ──
    first = connect(summary="does things", name="Ada", channel="chan-i",
                    model="sonnet")
    aid = first["member_id"]
    check("connect succeeds and returns a member_id", bool(aid))
    secret = first.get("reclaim_secret")
    check("a reclaim_secret is returned to the agent that minted the identity",
          isinstance(secret, str) and len(secret) > 20)

    row = agent_row(aid)
    check("a durable row lands in the global agents registry", row is not None)
    check("it is marked UNMANAGED — nothing supervises this agent",
          row is not None and row["managed"] == 0)
    check("name and model are carried onto the agents row",
          row["name"] == "Ada" and row["model"] == "sonnet")
    check("the stored secret is exactly the one handed to the agent",
          row["reclaim_secret"] == secret)

    # ── the secret is disclosed once, at mint ──
    again = connect(summary="does things", name="Ada", channel="chan-i",
                    resume_member_id=aid, reclaim_secret=secret)
    check("a correct reclaim re-attaches to the SAME identity",
          again.get("member_id") == aid)
    check("and reports itself as a reclaim, not a fresh join",
          again.get("action") == "reclaimed")
    check("a reclaim does NOT echo the secret back — member_id is public, so "
          "echoing it would let anyone reading the roster harvest it",
          again.get("reclaim_secret") == "")
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    members = conn.execute(
        "SELECT COUNT(*) FROM members WHERE channel='chan-i'").fetchone()[0]
    conn.close()
    check("a reclaim creates no second agents row", count == 1)
    check("and no second members row", members == 1)
    check("a silent re-attach posts no [joined] message — a restarting agent "
          "rejoining a channel it never left is not news",
          len(joins("chan-i")) == 1)

    # ── refusals ──
    bad = connect(summary="x", name="Mallory", channel="chan-i",
                  resume_member_id=aid, reclaim_secret="wrong-secret")
    check("a reclaim with the WRONG secret is refused outright",
          "error" in bad and "reclaim_secret" in bad["error"])
    none = connect(summary="x", name="Mallory", channel="chan-i",
                   resume_member_id=aid)
    check("a reclaim with NO secret is refused", "error" in none)
    check("neither refusal disturbed the real identity",
          agent_row(aid)["reclaim_secret"] == secret)

    # An unknown id must not be honoured — otherwise a caller could name any
    # identity it liked on a first join and simply be given it.
    ghost = connect(summary="x", name="Ghost", channel="chan-i",
                    resume_member_id="zzzzzz", reclaim_secret="anything")
    check("an unknown reclaim id mints a FRESH identity instead of granting it",
          ghost.get("member_id") not in ("zzzzzz", aid)
          and bool(ghost.get("member_id")))
    check("and that fresh identity gets its own distinct secret",
          ghost.get("reclaim_secret") not in ("", secret))

    # ── a human's row is not reclaimable ──
    # A web operator is a member with kind='human'. Its member_id is on the
    # public roster; if it were reclaimable, any MCP caller could mint a valid
    # session token as that operator and read their DMs.
    conn = db()
    conn.execute(
        "INSERT INTO members (id, channel, name, summary, skills, kind, "
        "last_seen, joined_at) VALUES ('_op_hu', 'chan-i', 'Operator', '', '', "
        "'human', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.commit()
    conn.close()
    stolen = connect(summary="x", name="Mallory", channel="chan-i",
                     resume_member_id="_op_hu", reclaim_secret="anything")
    check("a human/operator row cannot be reclaimed",
          "error" in stolen and "Cannot reclaim" in stolen["error"])

    # ── the INSERT is the authority, not the pre-check ──
    # This is the security-critical property and it needs a forced collision:
    # random 6-char ids will not collide on their own. Pin the generator so the
    # second connect proposes an id that is already taken. The loser of that
    # race must mint a NEW identity — it must never be handed the winner's
    # reclaim_secret, which is the whole credential for speaking as them.
    real_gen = srv.generate_member_id
    taken = agent_row(aid)
    proposals = iter([aid, aid, "fresh1"])

    def _collide():
        try:
            return next(proposals)
        except StopIteration:
            return real_gen()

    srv.generate_member_id = _collide
    try:
        loser = connect(summary="x", name="Loser", channel="chan-race")
    finally:
        srv.generate_member_id = real_gen
    check("a colliding mint retries instead of failing",
          "error" not in loser and bool(loser.get("member_id")))
    check("the loser of an id race gets a DIFFERENT id",
          loser["member_id"] != aid)
    check("and is never handed the winner's reclaim_secret",
          loser["reclaim_secret"] != taken["reclaim_secret"]
          and loser["reclaim_secret"] != "")
    check("the winner's stored identity is untouched by the race",
          agent_row(aid)["reclaim_secret"] == taken["reclaim_secret"]
          and agent_row(aid)["name"] == "Ada")

    # The pre-check above is NOT what makes this safe — the INSERT is. To reach
    # the INSERT's collision path the pre-check has to miss, which is exactly
    # the race window a real concurrent connect opens. Simulate it: a proxy
    # that answers the first collision-probe with "free" while the row really
    # exists, so the INSERT is the only thing standing between the caller and
    # someone else's credential.
    conn = db()
    conn.execute(
        "INSERT INTO agents (id, name, model, managed, reclaim_secret, "
        "created_at, last_active_at) VALUES ('dupe01', 'Winner', '', 0, "
        "'winner-secret', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.commit()

    class BlindOnce:
        """Passes everything through, but lies once about the collision probe."""

        def __init__(self, real):
            self._real = real
            self._lied = False

        def execute(self, sql, params=()):
            if not self._lied and "UNION ALL SELECT 1 FROM members" in sql:
                self._lied = True

                class _Empty:
                    def fetchone(self_inner):
                        return None
                return _Empty()
            return self._real.execute(sql, params)

    proposals2 = iter(["dupe01", "after-race"])
    srv.generate_member_id = lambda: next(proposals2, "fallback")
    try:
        got_id, got_secret = srv._register_agent_identity(
            BlindOnce(conn), "Racer", "", srv.now_iso())
    finally:
        srv.generate_member_id = real_gen
    conn.commit()
    check("when the pre-check misses, the INSERT collision is retried",
          got_id != "dupe01")
    check("and the loser is NEVER handed the existing row's secret — this is "
          "the property the INSERT-authoritative design exists for",
          got_secret != "winner-secret" and bool(got_secret))
    check("the existing row is left exactly as it was",
          agent_row("dupe01")["reclaim_secret"] == "winner-secret"
          and agent_row("dupe01")["name"] == "Winner")
    conn.close()

    # ── defence in depth: a human row shadowing a registered agent id ──
    # The early reclaim check refuses an UNKNOWN id whose channel row is human.
    # This is the other case: the id IS registered with a valid secret, but the
    # members row in the target channel is a human's. Contrived, but it is the
    # difference between one check and two, and the cost of the second is a
    # single SELECT.
    conn = db()
    conn.execute(
        "INSERT INTO agents (id, name, model, managed, reclaim_secret, "
        "created_at, last_active_at) VALUES ('shadow', 'Shadow', '', 0, "
        "'shadow-secret', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.execute(
        "INSERT INTO members (id, channel, name, summary, skills, kind, "
        "last_seen, joined_at) VALUES ('shadow', 'chan-shadow', 'Operator', "
        "'', '', 'human', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.execute(
        "INSERT INTO channels (code, status, created_at, updated_at) "
        "VALUES ('chan-shadow', 'active', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.commit()
    conn.close()
    shadowed = connect(summary="x", name="Mallory", channel="chan-shadow",
                       resume_member_id="shadow", reclaim_secret="shadow-secret")
    check("a registered id whose channel row is HUMAN is still refused, even "
          "with the correct secret",
          "error" in shadowed and "Cannot reclaim" in shadowed["error"])

    # ── capacity ──
    # A reclaim of a row you ALREADY hold must skip the capacity gate: that row
    # is already inside the count, so counting it against you would refuse a
    # placed agent entry to a seat it is still sitting in.
    conn = db()
    have = conn.execute(
        "SELECT COUNT(*) FROM members WHERE channel='chan-i'").fetchone()[0]
    for i in range(srv.MAX_MEMBERS - have):
        conn.execute(
            "INSERT INTO members (id, channel, name, summary, skills, "
            "last_seen, joined_at) VALUES (?, 'chan-i', ?, '', '', ?, ?)",
            (f"pad{i:03d}", f"Pad{i}", srv.now_iso(), srv.now_iso()))
    conn.commit()
    full = conn.execute(
        "SELECT COUNT(*) FROM members WHERE channel='chan-i'").fetchone()[0]
    conn.close()
    check(f"fixture: the channel is at MAX_MEMBERS ({srv.MAX_MEMBERS})",
          full == srv.MAX_MEMBERS)

    newcomer = connect(summary="x", name="Newcomer", channel="chan-i")
    check("a NEW member is refused when the channel is full",
          "error" in newcomer and "full" in newcomer["error"].lower())
    rejoin = connect(summary="does things", name="Ada", channel="chan-i",
                     resume_member_id=aid, reclaim_secret=secret)
    check("but an agent reclaiming its OWN existing row is let back in",
          rejoin.get("member_id") == aid and "error" not in rejoin)

    # A reclaim into a channel where the agent has NO row yet is a new seat,
    # so it must respect the ceiling. This is the assertion that distinguishes
    # "skip the gate for a row you already hold" from "skip the gate for every
    # reclaim" — without it, dropping `reclaimed_existing` entirely would leave
    # every other check in this file passing.
    other = connect(summary="x", name="Ada", channel="chan-elsewhere",
                    resume_member_id=aid, reclaim_secret=secret)
    check("a reclaim into an EMPTY channel it never joined creates the row",
          other.get("member_id") == aid and "error" not in other)
    conn = db()
    conn.execute("INSERT INTO channels (code, status, created_at, updated_at) "
                 "VALUES ('chan-packed', 'active', ?, ?)",
                 (srv.now_iso(), srv.now_iso()))
    for i in range(srv.MAX_MEMBERS):
        conn.execute(
            "INSERT INTO members (id, channel, name, summary, skills, "
            "last_seen, joined_at) VALUES (?, 'chan-packed', ?, '', '', ?, ?)",
            (f"pk{i:04d}", f"Pk{i}", srv.now_iso(), srv.now_iso()))
    conn.commit()
    conn.close()
    packed = connect(summary="x", name="Ada", channel="chan-packed",
                     resume_member_id=aid, reclaim_secret=secret)
    check("but a reclaim into a FULL channel it never joined is refused — the "
          "capacity skip is for a seat you already occupy, not a free pass",
          "error" in packed and "full" in packed["error"].lower())

    # ── cull retires the global identity too ──
    # Without this the culled agent keeps a durable id AND its reclaim_secret,
    # so it can walk straight back in — and nothing else ever deletes an
    # unmanaged row, so the registry grows forever.
    culled = connect(summary="x", name="Doomed", channel="chan-cull")
    host = connect(summary="x", name="Warden", channel="chan-cull")
    cid = culled["member_id"]
    check("fixture: the culled agent has a registered identity",
          agent_row(cid) is not None)
    json.loads(srv.nth_cull(channel="chan-cull", member_id=host["member_id"],
                            target_member_id=cid))
    check("cull removes the global identity of a self-connected agent",
          agent_row(cid) is None)
    conn = db()
    inbox = conn.execute(
        "SELECT 1 FROM members WHERE id = ? AND channel = ?",
        (cid, srv.AGENT_INBOX_CHANNEL)).fetchone()
    live = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE member_id = ? "
        "AND revoked_at IS NULL", (cid,)).fetchone()[0]
    conn.close()
    check("and its DM inbox presence, which is what authorises reading a DM",
          inbox is None)
    check("and every session it held is revoked", live == 0)

    # A managed agent is the operator's, not the channel's: culling it from one
    # channel must not delete the identity the roster is built on.
    conn = db()
    conn.execute(
        "INSERT INTO agents (id, name, model, managed, reclaim_secret, "
        "created_at, last_active_at) VALUES ('mg0001', 'Managed', '', 1, "
        "'m-secret', ?, ?)", (srv.now_iso(), srv.now_iso()))
    for ch in ("chan-cull", srv.AGENT_INBOX_CHANNEL):
        conn.execute(
            "INSERT INTO members (id, channel, name, summary, skills, "
            "last_seen, joined_at) VALUES ('mg0001', ?, 'Managed', '', '', ?, ?)",
            (ch, srv.now_iso(), srv.now_iso()))
    # A session in ANOTHER channel: culling from chan-cull legitimately revokes
    # that channel's sessions, so only a session elsewhere can show whether the
    # revoke was wrongly widened to every session the member holds.
    conn.execute(
        "INSERT INTO sessions (session_token, member_id, channel, role, "
        "fingerprint, connected_at, last_seen, last_read) VALUES "
        "('mgtok', 'mg0001', 'chan-other', 'primary', 'f', ?, ?, 0)",
        (srv.now_iso(), srv.now_iso()))
    conn.commit()
    conn.close()
    json.loads(srv.nth_cull(channel="chan-cull", member_id=host["member_id"],
                            target_member_id="mg0001"))
    conn = db()
    mg_inbox = conn.execute(
        "SELECT 1 FROM members WHERE id='mg0001' AND channel=?",
        (srv.AGENT_INBOX_CHANNEL,)).fetchone()
    mg_sessions = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE member_id='mg0001' "
        "AND revoked_at IS NULL").fetchone()[0]
    conn.close()
    check("a MANAGED agent's identity survives being culled from a channel",
          agent_row("mg0001") is not None)
    # The identity surviving is not enough. An earlier version of the
    # retirement block guarded only the DELETE with managed=0 and left the
    # inbox delete and the session revoke unguarded — so a managed agent kept
    # its roster row but lost the presence that makes it messageable, and DMs
    # to it silently failed until the next hub start. Assert the whole block
    # was skipped, not just its last statement.
    check("...along with its inbox presence, which is what makes it "
          "messageable at all", mg_inbox is not None)
    check("...and its live sessions, which a channel-scoped cull must not "
          "revoke globally", mg_sessions == 1)

    # A human is not an identity this retires at all. Culling an operator from
    # a channel must never escalate to signing them out everywhere.
    conn = db()
    conn.execute(
        "INSERT INTO members (id, channel, name, summary, skills, kind, "
        "last_seen, joined_at) VALUES ('_op_vic', 'chan-cull', 'Victim', '', "
        "'', 'human', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.execute(
        "INSERT INTO members (id, channel, name, summary, skills, kind, "
        "last_seen, joined_at) VALUES ('_op_vic', ?, 'Victim', '', '', "
        "'human', ?, ?)", (srv.AGENT_INBOX_CHANNEL, srv.now_iso(), srv.now_iso()))
    # Again in another channel, for the same reason.
    conn.execute(
        "INSERT INTO sessions (session_token, member_id, channel, role, "
        "fingerprint, connected_at, last_seen, last_read) VALUES "
        "('optok', '_op_vic', 'chan-other', 'primary', 'f', ?, ?, 0)",
        (srv.now_iso(), srv.now_iso()))
    conn.commit()
    conn.close()
    json.loads(srv.nth_cull(channel="chan-cull", member_id=host["member_id"],
                            target_member_id="_op_vic"))
    conn = db()
    op_inbox = conn.execute(
        "SELECT 1 FROM members WHERE id='_op_vic' AND channel=?",
        (srv.AGENT_INBOX_CHANNEL,)).fetchone()
    op_sessions = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE member_id='_op_vic' "
        "AND revoked_at IS NULL").fetchone()[0]
    conn.close()
    check("culling a HUMAN from a channel does not strip their inbox row",
          op_inbox is not None)
    check("nor revoke every session they hold — a channel-scoped removal must "
          "not escalate to a global sign-out at a peer agent's request",
          op_sessions == 1)

    # ── the global name follows a rename on EVERY reclaim path ──
    # A stale agents.name is a second, hidden @handle: the sigil resolver
    # merges global names into each member's wake candidates, so it still wakes
    # the agent while appearing on no roster. The mint-time name is
    # caller-supplied, which makes that alias attacker-choosable.
    renamer = connect(summary="x", name="Gabe", channel="chan-rename")
    rid, rsecret = renamer["member_id"], renamer["reclaim_secret"]
    connect(summary="x", name="helper", channel="chan-rename",
            resume_member_id=rid, reclaim_secret=rsecret)
    check("a rename on reclaim into an EXISTING channel updates agents.name",
          agent_row(rid)["name"] == "helper")
    # The branch that was missed: reclaiming into a channel that does not exist
    # yet takes the new-channel path.
    connect(summary="x", name="scout", channel="chan-rename-fresh",
            resume_member_id=rid, reclaim_secret=rsecret)
    check("and so does a reclaim into a channel that did not exist yet",
          agent_row(rid)["name"] == "scout")
    _m, _r, bangs = srv._parse_sigils(db(), "chan-rename-fresh", "@Gabe hello")
    mentions, _r2, _b2 = srv._parse_sigils(
        db(), "chan-rename-fresh", "@Gabe hello")
    check("so the old name is no longer a live @handle anywhere",
          rid not in mentions)

    # ── the mint loop is bounded ──
    check("the mint retry has a finite cap, so a schema change that makes a "
          "non-PK constraint fire cannot spin forever",
          isinstance(srv.MAX_IDENTITY_MINT_ATTEMPTS, int)
          and 0 < srv.MAX_IDENTITY_MINT_ATTEMPTS <= 64)
    _always = lambda: "always-the-same"
    _real = srv.generate_member_id
    srv.generate_member_id = _always
    conn = db()
    conn.execute(
        "INSERT INTO agents (id, name, model, managed, reclaim_secret, "
        "created_at, last_active_at) VALUES ('always-the-same', 'Squatter', "
        "'', 0, 's', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.commit()
    try:
        raised = False
        try:
            srv._register_agent_identity(conn, "Doomed", "", srv.now_iso())
        except RuntimeError:
            raised = True
        check("a generator that can only ever collide gives up instead of "
              "looping forever", raised)
    finally:
        srv.generate_member_id = _real
        conn.close()

    # ── the hub must not resurrect presence it does not own ──
    # ensure_agent_inboxes runs on every hub start and force-sets active=1 on
    # the inbox row of every agent it finds. Once self-connected agents have
    # rows in `agents`, an unfiltered sweep would undo any deactivation — and
    # inbox presence is exactly what authorises reading a DM addressed to you.
    ghost2 = connect(summary="x", name="Ghost2", channel="chan-ghost")
    gid = ghost2["member_id"]
    conn = db()
    conn.execute("UPDATE members SET active = 0 WHERE id = ? AND channel = ?",
                 (gid, srv.AGENT_INBOX_CHANNEL))
    conn.commit()
    before = conn.execute(
        "SELECT active FROM members WHERE id = ? AND channel = ?",
        (gid, srv.AGENT_INBOX_CHANNEL)).fetchone()
    check("fixture: the self-connected agent's inbox presence is deactivated",
          before is not None and before["active"] == 0)
    web.ensure_agent_inboxes(conn)
    conn.commit()
    after = conn.execute(
        "SELECT active FROM members WHERE id = ? AND channel = ?",
        (gid, srv.AGENT_INBOX_CHANNEL)).fetchone()
    conn.close()
    check("a hub restart does NOT reactivate a self-connected agent's inbox "
          "presence — the hub does not own it",
          after is not None and after["active"] == 0)

    # The same sweep MUST still do its job for a managed agent, which is what
    # it exists for.
    conn = db()
    conn.execute(
        "INSERT INTO agents (id, name, model, managed, reclaim_secret, "
        "created_at, last_active_at) VALUES ('mg0002', 'Managed2', '', 1, "
        "'m2', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.commit()
    web.ensure_agent_inboxes(conn)
    conn.commit()
    placed = conn.execute(
        "SELECT active FROM members WHERE id = 'mg0002' AND channel = ?",
        (srv.AGENT_INBOX_CHANNEL,)).fetchone()
    conn.close()
    check("but it still places a MANAGED agent in its inbox, as intended",
          placed is not None and placed["active"] == 1)

    # ── a self-connected agent must not squat the operator's name pool ──
    # pick_agent_name draws spawned-agent names from `agents`, so without a
    # managed filter an agent that names itself "Scout" silently removes that
    # character name from the supervisor's pool and blocks an operator asking
    # for it.
    # Assert THROUGH pick_agent_name: ask it for the exact name the
    # self-connected agent took. `desired` is returned when it is not in the
    # blocked set, so this fails iff the self-connected row entered the pool.
    squatted = web._CHARACTER_NAMES[0]
    connect(summary="x", name=squatted, channel="chan-names")
    conn = db()
    try:
        granted = web.pick_agent_name(conn, desired=squatted)
        # And the converse, so the check cannot pass by the function ignoring
        # `desired` altogether: a MANAGED row with that name must block it.
        conn.execute(
            "INSERT INTO agents (id, name, model, managed, reclaim_secret, "
            "created_at, last_active_at) VALUES ('nm0001', ?, '', 1, 'x', ?, ?)",
            (squatted, srv.now_iso(), srv.now_iso()))
        conn.commit()
        refused = web.pick_agent_name(conn, desired=squatted)
    finally:
        conn.close()
    check("a self-connected agent taking a character name does not block the "
          "operator from requesting it", granted == squatted)
    check("...but a MANAGED agent holding it does", refused != squatted)

    # ── a shadowed identity is not retired by cull ──
    # An id with an unmanaged `agents` row AND a kind='human' members row is a
    # contradictory state. Cull declines to retire it rather than guessing,
    # which is what the NOT EXISTS in the eligibility test is for.
    conn = db()
    conn.execute(
        "INSERT INTO agents (id, name, model, managed, reclaim_secret, "
        "created_at, last_active_at) VALUES ('shadowcull', 'Shadow2', '', 0, "
        "'s2', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.execute(
        "INSERT INTO members (id, channel, name, summary, skills, kind, "
        "last_seen, joined_at) VALUES ('shadowcull', 'chan-cull', 'Shadow2', "
        "'', '', 'human', ?, ?)", (srv.now_iso(), srv.now_iso()))
    conn.commit()
    conn.close()
    json.loads(srv.nth_cull(channel="chan-cull", member_id=host["member_id"],
                            target_member_id="shadowcull"))
    check("an id whose members row says human is not retired, even with an "
          "unmanaged agents row", agent_row("shadowcull") is not None)

    # ── no two identities can ever share an id or a secret ──
    ids, secrets_seen = {aid, ghost["member_id"]}, {secret, ghost["reclaim_secret"]}
    # One channel each: MAX_MEMBERS is 20, and a "channel full" refusal here
    # would be measuring the wrong thing entirely.
    for i in range(25):
        r = connect(summary="x", name=f"Bot{i}", channel=f"chan-m{i}")
        assert "error" not in r, r
        ids.add(r["member_id"])
        secrets_seen.add(r["reclaim_secret"])
    check("25 more connects produce 27 distinct ids", len(ids) == 27)
    check("and 27 distinct secrets", len(secrets_seen) == 27)
    conn = db()
    dupes = conn.execute(
        "SELECT reclaim_secret, COUNT(*) c FROM agents GROUP BY reclaim_secret "
        "HAVING c > 1").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    unmanaged = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE managed = 0").fetchone()[0]
    managed_fixtures = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE managed = 1").fetchone()[0]
    conn.close()
    check("no secret is shared between two agents rows", not dupes)
    # Counted rather than hardcoded: the race and shadow fixtures above also
    # add rows, and a hardcoded total would have to be edited every time a
    # test is added — which is how an assertion quietly stops asserting.
    # Every row created by CONNECT is unmanaged; the one managed row is the
    # fixture inserted above to prove cull spares it.
    # Counted, not hardcoded: the managed rows are fixtures inserted above to
    # prove cull and the inbox sweep treat them differently, and a hardcoded
    # total would need editing every time a test is added — which is how an
    # assertion quietly stops asserting.
    check("every agents row created by connect is unmanaged",
          total > 27 and unmanaged == total - managed_fixtures
          and managed_fixtures > 0)
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("OK — self-connected agents get a durable, reclaimable identity")
