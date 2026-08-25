"""A spawned agent must reclaim its pre-assigned identity, not mint a new one.

The hub creates an `agents` row and a `members` row keyed on the agent id, then
tells the agent that id in its launch preamble. If trio_connect ignores it and
mints a fresh member_id, the agent silently becomes a SECOND member and three
things break at once, none of them loudly:

  * the router's "never feed an agent its own message" check compares against
    the agent id, stops matching, and the agent is fed its own output — which
    with an ambient wake mode is a self-sustaining loop;
  * the reply-dedup probe (also keyed on agent id) stops matching, so replies
    duplicate;
  * the roster and liveness map never see the agent's heartbeat, so a healthy
    agent reads as offline.

Also covers the secret itself: it is rotated on every spawn, so one leaked from
an old process or an old transcript must not work.

Usage: python tests/test-agent-reclaim.py
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
import nth_server as srv  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="nth-reclaim-"))
srv.DB_DIR = tmp
srv.DB_PATH = tmp / "nth.db"

# A channel, and an agents row the hub would have created before spawning.
srv.nth_connect(summary="host", name="Host", channel="room")
db = sqlite3.connect(str(srv.DB_PATH))
db.execute("INSERT INTO agents (id, name, model, state, managed, created_at, "
           "reclaim_secret) VALUES ('ag_ayla','Ayla','sonnet','spawning',1,?,?)",
           (srv.now_iso(), "SECRET-AT-SPAWN"))
db.commit()
db.close()


def members_named(name):
    c = sqlite3.connect(str(srv.DB_PATH))
    try:
        return [r[0] for r in c.execute(
            "SELECT id FROM members WHERE channel='room' AND name=?", (name,))]
    finally:
        c.close()


# ── the reclaim itself ──────────────────────────────────────────────────────
r = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                               resume_member_id="ag_ayla",
                               reclaim_secret="SECRET-AT-SPAWN"))
check("reclaim returns the PRE-ASSIGNED id, not a fresh one",
      r.get("member_id") == "ag_ayla")
check("exactly one members row exists for the agent",
      members_named("Ayla") == ["ag_ayla"])

# Reconnecting (a wake, a restart) re-attaches to the SAME row.
r2 = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                resume_member_id="ag_ayla",
                                reclaim_secret="SECRET-AT-SPAWN"))
check("a second connect re-attaches rather than duplicating",
      r2.get("member_id") == "ag_ayla" and members_named("Ayla") == ["ag_ayla"])


# ── the secret is load-bearing ──────────────────────────────────────────────
bad = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                 resume_member_id="ag_ayla",
                                 reclaim_secret="WRONG"))
check("a wrong secret is refused", "error" in bad)
check("...and the refusal does not leak the real secret",
      "SECRET-AT-SPAWN" not in json.dumps(bad))

none = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                  resume_member_id="ag_ayla"))
check("a missing secret is refused", "error" in none)

# Rotation: the hub mints a fresh secret on every spawn, so the old one dies.
db = sqlite3.connect(str(srv.DB_PATH))
db.execute("UPDATE agents SET reclaim_secret='SECRET-AFTER-RESPAWN' WHERE id='ag_ayla'")
db.commit()
db.close()
stale = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                   resume_member_id="ag_ayla",
                                   reclaim_secret="SECRET-AT-SPAWN"))
check("a secret from a previous spawn no longer works", "error" in stale)
fresh = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                   resume_member_id="ag_ayla",
                                   reclaim_secret="SECRET-AFTER-RESPAWN"))
check("the current secret does", fresh.get("member_id") == "ag_ayla")


# ── an id that is not a registered agent is never handed over ───────────────
host_id = members_named("Host")[0] if members_named("Host") else None
took = json.loads(srv.nth_connect(summary="x", name="Impostor", channel="room",
                                  resume_member_id=host_id,
                                  reclaim_secret="anything"))
# Host connected itself over MCP, so it now HAS a registered global identity —
# which makes this a wrong-secret reclaim of a registered id, and those are
# refused outright rather than quietly handed a fresh one. That is stricter
# than the old fallback: before self-connected agents had a durable identity,
# an impostor naming this id simply got a new one and no signal that it had
# tried to take someone else's.
check("claiming a registered member's id does NOT return that id",
      took.get("member_id") != host_id)
check("...and is refused outright, not silently given a fresh identity",
      "error" in took and "reclaim_secret" in took["error"])

unknown = json.loads(srv.nth_connect(summary="x", name="Ghost", channel="room",
                                     resume_member_id="ag_does_not_exist",
                                     reclaim_secret="anything"))
check("an unknown agent id does not become that id",
      unknown.get("member_id") != "ag_does_not_exist")


# ── a reclaim DISPLACES the incumbent session ───────────────────────────────
# Rotation alone only guards the door. Two supervisors sharing a DB each
# rotated the secret and spawned the same agent seconds apart, and both held
# live primary sessions for one member_id — one agent answering every mention
# twice, from two processes, for 18 hours. A token, once minted, stayed valid
# until the week-long reap. So the winning reclaim has to revoke the loser.
first = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                   resume_member_id="ag_ayla",
                                   reclaim_secret="SECRET-AFTER-RESPAWN"))
first_token = first.get("session_token")
check("a reclaim hands back a session token", bool(first_token))

# The agent also holds a session on a SECOND channel. Reclaiming "room" must
# not sever it: a session is scoped to (member_id, channel).
srv.nth_connect(summary="a", name="Ayla", channel="other",
                resume_member_id="ag_ayla",
                reclaim_secret="SECRET-AFTER-RESPAWN")
other = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="other",
                                   resume_member_id="ag_ayla",
                                   reclaim_secret="SECRET-AFTER-RESPAWN"))
other_token = other.get("session_token")

# The twin spawns and reclaims the same identity in "room".
second = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                    resume_member_id="ag_ayla",
                                    reclaim_secret="SECRET-AFTER-RESPAWN"))
second_token = second.get("session_token")
check("the second reclaim gets a DIFFERENT token",
      bool(second_token) and second_token != first_token)

stale_send = json.loads(srv.nth_send("room", "ag_ayla", "from the stale twin",
                                     session_token=first_token))
check("the displaced session can no longer send",
      "error" in stale_send and "session_token" in stale_send["error"])

live_send = json.loads(srv.nth_send("room", "ag_ayla", "from the live agent",
                                    session_token=second_token))
check("the current session still can", "error" not in live_send)

cross = json.loads(srv.nth_send("other", "ag_ayla", "still here",
                                session_token=other_token))
check("a reclaim in one channel does not revoke the agent's session in another",
      "error" not in cross)


# ── displacement does NOT depend on a members row ───────────────────────────
# The revoke is gated on `reclaiming` alone, not on "a members row exists".
# Those come apart, and the gap was reachable from a dashboard button:
# nth_web._remove_from_channel deletes the members row but revokes sessions only
# when no presence remains ANYWHERE, so a multi-channel agent removed from one
# room keeps a live primary token for it. Gating on the members row meant the
# reclaim back in skipped the revoke and left two live tokens on one identity —
# the exact state this whole change exists to prevent. (nth_purge is a second
# instance: it drops members and channels rows and never touches sessions.)
srv.nth_connect(summary="a", name="Ayla", channel="elsewhere",
                resume_member_id="ag_ayla",
                reclaim_secret="SECRET-AFTER-RESPAWN")
orphan = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                    resume_member_id="ag_ayla",
                                    reclaim_secret="SECRET-AFTER-RESPAWN"))
orphan_token = orphan["session_token"]

# Exactly what _remove_from_channel does when presence remains elsewhere:
# members row gone, session left live.
c = sqlite3.connect(str(srv.DB_PATH))
c.execute("DELETE FROM members WHERE id='ag_ayla' AND channel='room'")
c.commit()
c.close()

back = json.loads(srv.nth_connect(summary="a", name="Ayla", channel="room",
                                  resume_member_id="ag_ayla",
                                  reclaim_secret="SECRET-AFTER-RESPAWN"))
check("reclaiming into a channel whose members row was removed still works",
      back.get("member_id") == "ag_ayla" and back.get("session_token"))
stale = json.loads(srv.nth_send("room", "ag_ayla", "orphaned twin",
                                session_token=orphan_token))
check("...and STILL displaces the incumbent, members row or not",
      "error" in stale and "session_token" in stale["error"])

c = sqlite3.connect(str(srv.DB_PATH))
live = c.execute(
    "SELECT COUNT(*) FROM sessions WHERE member_id='ag_ayla' AND channel='room' "
    "AND role='primary' AND revoked_at IS NULL").fetchone()[0]
c.close()
check("...leaving exactly one live primary session for the identity",
      live == 1)


# ── ordinary callers are unaffected ─────────────────────────────────────────
plain = json.loads(srv.nth_connect(summary="p", name="Plain", channel="room"))
check("a connect with no resume_member_id still mints an id",
      bool(plain.get("member_id")) and plain["member_id"] != "ag_ayla")

# An ordinary connect mints a fresh member_id, so it has no incumbent to
# displace — and must not touch anyone else's live session. Uses the CURRENT
# token: every reclaim above displaced the one before it, so `second_token` is
# long dead by now and asserting on it would test the displacement sweep again
# rather than this.
srv.nth_connect(summary="p2", name="Plain2", channel="room")
after_plain = json.loads(srv.nth_send("room", "ag_ayla", "unaffected",
                                      session_token=back["session_token"]))
check("a non-reclaiming connect leaves other members' sessions alone",
      "error" not in after_plain)

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")
