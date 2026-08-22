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


# ── ordinary callers are unaffected ─────────────────────────────────────────
plain = json.loads(srv.nth_connect(summary="p", name="Plain", channel="room"))
check("a connect with no resume_member_id still mints an id",
      bool(plain.get("member_id")) and plain["member_id"] != "ag_ayla")

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")
