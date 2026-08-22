"""The agent router must not let managed agents wake each other.

Ambient wake modes ("all", and "about" via #refs) exist so an agent notices
what HUMANS are saying around it. Applied to another agent's output they are a
self-sustaining loop: A posts, B wakes and replies, which wakes A, which wakes
B. Every hop is a real billed turn and nothing in the loop decides to stop.

This was reproduced against the real router before the guard: two fake agents
in one channel with wake_mode="all", one ordinary human message, and the
messages table grew ~4 rows every 0.5s indefinitely.

Agent-to-agent traffic must therefore be EXPLICIT — an @mention, a !bang, or a
direct message. Those are deliberate single acts, not something that fires
repeatedly on its own.

Usage: python tests/test-agent-router-loop.py
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
import nth_web as web      # noqa: E402
import nth_server as srv   # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="nth-router-loop-"))
srv.DB_DIR = tmp
srv.DB_PATH = tmp / "nth.db"
DB = srv.DB_PATH

# Real schema, then two managed agents placed in one channel plus a human.
srv.nth_connect(summary="t", name="Human", channel="room")
db = sqlite3.connect(str(DB))
db.row_factory = sqlite3.Row
now = srv.now_iso()
for aid, name in (("ag_a", "Ayla"), ("ag_b", "Bram")):
    db.execute("INSERT INTO agents (id, name, model, state, managed, created_at, "
               "wake_mode) VALUES (?,?,'sonnet','running',1,?,'all')", (aid, name, now))
    db.execute("INSERT INTO members (id, channel, name, summary, skills, last_seen, "
               "joined_at, active) VALUES (?,?,?,'','',?,?,1)",
               (aid, "room", name, now, now))
    db.execute("INSERT INTO agent_channels (agent_id, channel, member_id, joined_at) "
               "VALUES (?,?,?,?)", (aid, "room", aid, now))
# A real human: a web operator row, kind='human', with NO row in `agents`.
# The MCP-connected "Human" above is not one — an agent that connects itself
# now registers a durable agents identity, so it is correctly classified as an
# agent for wake purposes. Using it as the human stand-in would test the
# opposite of what this file is about.
human = "_op_human"
db.execute("INSERT INTO members (id, channel, name, summary, skills, kind, "
           "last_seen, joined_at, active) VALUES (?,?,?,'','','human',?,?,1)",
           (human, "room", "Operator", now, now))
self_connected = db.execute(
    "SELECT id FROM members WHERE channel='room' AND id NOT LIKE 'ag_%' "
    "AND id != ?", (human,)).fetchone()["id"]
db.commit()
db.close()

router = web.AgentRouter(DB, supervisor=None)


def targets_for(sender_id, content="hello", mentions=None, bangs=None,
                recipients=None, wake_mode="all"):
    """What the REAL _targets returns for a message from sender_id."""
    row = {
        "id": 1, "channel": "room", "member_id": sender_id, "content": content,
        "mentions": json.dumps(mentions or []),
        "refs": json.dumps([]),
        "bangs": json.dumps(bangs or []),
        "recipients": json.dumps(recipients or []),
    }

    class _Row(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    return router._targets(_Row(row), {"ag_a": wake_mode, "ag_b": wake_mode})


# ── the loop itself ─────────────────────────────────────────────────────────
t = targets_for(human)
check("a human's ambient message wakes both agents (the feature still works)",
      t == {"ag_a", "ag_b"})

t = targets_for("ag_a")
check("an AGENT's ambient message wakes nobody (the loop is cut)", t == set())


# The case this project actually runs: a room of agents that connected
# THEMSELVES over MCP. Before self-connected agents had a durable identity they
# were indistinguishable from humans here, so the loop-cut did not apply to the
# most common deployment at all.
t_self = targets_for(self_connected)
check("a SELF-CONNECTED agent's ambient message also wakes nobody — the loop "
      "protection now covers the configuration it always should have",
      t_self == set())
t = targets_for("ag_a", wake_mode="about")
check("...also under 'about'", t == set())

# ── explicit address still gets through, in both directions ────────────────
t = targets_for("ag_a", mentions=["ag_b"])
check("agent->agent @mention IS delivered", t == {"ag_b"})
check("...and does not also wake the sender", "ag_a" not in t)

t = targets_for("ag_a", bangs=["ag_b"])
check("agent->agent !bang IS delivered", t == {"ag_b"})

t = targets_for("ag_a", recipients=["ag_b"])
check("agent->agent DM IS delivered", t == {"ag_b"})

# Under "at", only the mentioned agent wakes — this is the assertion that
# actually distinguishes targeting from "everyone wakes anyway".
t = targets_for(human, mentions=["ag_a"], wake_mode="at")
check("a human's @mention under 'at' targets ONLY that agent", t == {"ag_a"})
t = targets_for(human, wake_mode="at")
check("a human's ambient message under 'at' targets nobody", t == set())


# ── fail closed ─────────────────────────────────────────────────────────────
# If the agent roster cannot be read we must assume the sender IS an agent:
# a missed ambient wake is a delay, a loop is an unbounded bill.
_orig = router._agent_sender_ids
router._agent_sender_ids = lambda: None
try:
    t = targets_for(human)
    check("roster unreadable -> ambient wake suppressed (fails CLOSED)", t == set())
    t = targets_for(human, mentions=["ag_a"])
    check("roster unreadable -> explicit @mention still delivered", t == {"ag_a"})
finally:
    router._agent_sender_ids = _orig


# ── and the guard is not just the cache being empty ────────────────────────
# The self-connected agent is in this set too, and the operator is not — that
# split is exactly what the loop-cut turns on.
check("the guard reads real agent ids, self-connected agents included",
      router._agent_sender_ids() == {"ag_a", "ag_b", self_connected})
check("and a human operator is NOT in it",
      human not in router._agent_sender_ids())

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")
