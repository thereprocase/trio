"""A heartbeat tick is not a roster change.

EventHub re-broadcasts a channel's whole roster whenever its snapshot differs
from the last one. The snapshot used to be json.dumps(members), and members
carry `last_seen` — which nth_monitor.py rewrites every 10s FOR EVERY MEMBER,
with no message traffic at all. So the "only broadcast on change" guard almost
never suppressed anything: measured on this repo's own hub, the 52-member
agent-inbox re-sent a 23KB roster 10 times in 45 seconds on an idle channel,
and diffing consecutive emits showed exactly one changed field — one member's
last_seen. ~6KB/s, forever, to every connected browser, to redraw a heartbeat.

The fix is _roster_change_key(): drop the self-ticking fields from the
COMPARISON while leaving them in the payload. That is only safe because the
thing the UI actually paints is `status` — the coarse member_status() bucket —
which stays in the key. So the two properties that matter are opposite, and
both are asserted here:

  * a heartbeat-only tick must NOT broadcast (the bug), and
  * every real change, above all a liveness TRANSITION, still must (the
    regression that would make the fix worse than the bug — a roster frozen
    on a dead agent showing green).

Usage: python tests/test-roster-churn.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ.setdefault("NTH_HOME", tempfile.mkdtemp(prefix="nth_churn_"))

import nth_web as web    # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def member(**over):
    """A roster member shaped like _fetch_roster's output."""
    base = {
        "id": "ag_1", "name": "Ada", "kind": "agent", "status_text": "",
        "last_seen": "2026-08-30T12:00:00+00:00",
        "last_read": 40, "filter_mode": "all",
        "context_pct": 12.0, "context": None, "status": "active",
        "last_tool_name": "", "last_tool_target": "", "last_tool_at": "",
        "blocked_since": "", "stalled": None,
        "animal_name": "otter", "animal_emoji": "\U0001f9a6", "buddy": "",
    }
    base.update(over)
    return base


key = web._roster_change_key

# ── the bug: a bare heartbeat tick must not reach the wire ──
base = [member()]
tick = [member(last_seen="2026-08-30T12:00:10+00:00")]
check("a last_seen tick alone does not change the key",
      key(base) == key(tick))

# Same, with the nested context ring the statusline publisher embeds per
# member. Left unscrubbed these re-introduce the exact churn one level down,
# and the fix would be a no-op for any member with a live ring.
ctx = [member(context={"used_pct": 12.0, "_age_s": 3, "_relayed_at": "T1"})]
ctx_tick = [member(context={"used_pct": 12.0, "_age_s": 9, "_relayed_at": "T2"})]
check("a context ring aging alone does not change the key",
      key(ctx) == key(ctx_tick))

# That payload is NOT flat. project_context keeps harness.context_window and
# harness.rate_limits, and rate_limits is a rolling window whose percentage
# slides on its own — so a depth-1 scrub leaves self-ticking values two
# levels down and the churn returns for exactly the members a live ring
# makes most expensive to broadcast.
def ring(age, pct, used):
    return {"used_pct": pct, "_age_s": age,
            "harness": {"context_window": {"used": used},
                        "rate_limits": {"five_hour": {"used_percentage": 41.0}}}}


check("a nested _age_s two levels down does not change the key",
      key([member(context={"a": {"b": {"_age_s": 1, "keep": 1}}})])
      == key([member(context={"a": {"b": {"_age_s": 99, "keep": 1}}})]))
check("a rolling rate-limit window sliding does not change the key",
      key([member(context=ring(1, 12.0, 5))])
      == key([member(context={"used_pct": 12.0, "_age_s": 1,
                              "harness": {
                                  "context_window": {"used": 5},
                                  "rate_limits": {"five_hour": {
                                      "used_percentage": 88.0}}}})]))
check("a real context_window move under harness DOES change the key",
      key([member(context=ring(1, 12.0, 5))])
      != key([member(context=ring(1, 12.0, 9))]))

check("a heartbeat tick AND a ring aging together still do not",
      key([member(context={"used_pct": 5.0, "_age_s": 1})])
      == key([member(last_seen="2026-08-30T13:00:00+00:00",
                     context={"used_pct": 5.0, "_age_s": 55})]))

# ── the regression that would be worse: real changes must still broadcast ──
# A transition is the whole reason dropping the raw timestamp is safe. If
# member_status() flips to stale/dead and the key does not move, the browser
# paints a live dot on a dead agent indefinitely.
for status in ("stale", "dead", "working", "idle", "blocked"):
    check(f"a liveness transition to {status!r} still changes the key",
          key(base) != key([member(status=status)]))

for field, value in (
    ("last_read", 41),                       # watermark actually moved
    ("status_text", "sleeping"),
    ("name", "Ada2"),                        # rename
    ("last_tool_at", "2026-08-30T12:00:05+00:00"),   # real activity
    ("last_tool_name", "Bash"),
    ("blocked_since", "2026-08-30T12:00:05+00:00"),
    ("stalled", {"since": "T"}),
    ("context_pct", 13.0),
    ("filter_mode", "at"),
    ("kind", "human"),
):
    check(f"a change to {field!r} still changes the key",
          key(base) != key([member(**{field: value})]))

check("a real used_pct move inside the ring still changes the key",
      key(ctx) != key([member(context={"used_pct": 99.0, "_age_s": 3,
                                       "_relayed_at": "T1"})]))

# ── membership changes ──
check("a member joining changes the key",
      key(base) != key([member(), member(id="ag_2", name="Bo")]))
check("a member leaving changes the key",
      key([member(), member(id="ag_2", name="Bo")]) != key(base))

# ── the key must not mutate what it digests ──
# It is called on the live roster that is about to be broadcast; scrubbing in
# place would ship a payload with last_seen missing, and the client's own
# staleness math would have nothing to read.
live = [member(context={"used_pct": 1.0, "_age_s": 2})]
before = copy.deepcopy(live)
key(live)
check("the key does not mutate the roster it digests", live == before)
check("last_seen survives in the payload", "last_seen" in live[0])

# ── it must be stable, or it broadcasts on nothing at all ──
check("the key is order-insensitive within a member dict",
      key([member()]) == key([dict(reversed(list(member().items())))]))
check("the key is deterministic across calls", key(base) == key(base))

# ── the volatile set is what it claims to be ──
check("last_seen is declared volatile", "last_seen" in web._ROSTER_VOLATILE)
for keep in ("last_read", "status", "last_tool_at", "blocked_since"):
    check(f"{keep!r} is NOT treated as volatile",
          keep not in web._ROSTER_VOLATILE)

check("an empty roster produces a stable, non-crashing key", key([]) == "[]")

# The digest is order-SENSITIVE: json.dumps(sort_keys=True) sorts dict KEYS,
# not list order. That is only safe because _fetch_roster's SQL carries an
# ORDER BY. If that were ever weakened, this same roster would reorder every
# poll and the digest would flap on nothing — the churn this file exists to
# kill, arriving through the back door. Pin the assumption the function
# relies on but cannot itself guarantee.
check("the key is order-SENSITIVE, so callers must supply stable SQL order",
      key([member(id="ag_1"), member(id="ag_2", name="Bo")])
      != key([member(id="ag_2", name="Bo"), member(id="ag_1")]))


# ── the wiring: is the comparator actually IN the live poll loop? ──
# Everything above calls _roster_change_key directly. None of it proves
# EventHub.run() uses it rather than a bare json.dumps(members) a future
# edit could reintroduce — that revert would leave every check above green
# while restoring the bug in full. So drive the real background thread
# against a real DB and watch the wire.
import queue as _queue          # noqa: E402
import sqlite3 as _sqlite3      # noqa: E402

import nth_server as srv        # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="nth_churn_wire_"))
srv.DB_DIR, srv.DB_PATH = _tmp, _tmp / "nth.db"
_j = json.loads(srv.nth_connect(summary="wired", name="Wired", channel="wire-ch"))
_ch, _me = _j["channel"], _j["member_id"]

_hub = web.EventHub(srv.DB_PATH, _ch)
_hub.start()
_q = _hub.subscribe(include_history=False)


def _drain(seconds=web.DB_POLL_INTERVAL * 6):
    """Collect every payload the hub emits over the next few ticks."""
    out, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            out.append(json.loads(_q.get(timeout=0.1)))
        except _queue.Empty:
            continue
    return out


def _write(sql, params):
    conn = _sqlite3.connect(str(srv.DB_PATH), timeout=5)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


try:
    _drain()   # let the hub settle and emit its first-tick roster

    # A bare heartbeat tick — exactly what nth_monitor.py writes every 10s.
    _write("UPDATE members SET last_seen = ? WHERE channel = ? AND id = ?",
           (web.now_iso(), _ch, _me))
    check("the LIVE poll loop does not broadcast a heartbeat-only tick",
          not [e for e in _drain() if e.get("type") == "roster"])

    # Something that actually happened.
    _write("UPDATE members SET status_text = ? WHERE channel = ? AND id = ?",
           ("brb", _ch, _me))
    check("the LIVE poll loop still broadcasts a real roster change",
          [e for e in _drain() if e.get("type") == "roster"])

    # The initial snapshot must stay FULL. A plausible-looking "consistency"
    # refactor routing _prime_payloads through the same scrub would strip
    # last_seen from every client's first paint, and nothing else would
    # notice — the change key is not the payload.
    _primed = [json.loads(p) for p in
               _hub._prime_payloads(None, True, 0, include_history=False)]
    _roster = next((e for e in _primed if e.get("type") == "roster"), None)
    check("_prime_payloads ships an unscrubbed roster for first paint",
          bool(_roster) and bool(_roster["members"][0].get("last_seen")))
    check("_prime_payloads is not gated by the change key",
          bool(_roster))
finally:
    _hub.unsubscribe(_q)
    _hub.stop()
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("OK — heartbeat churn is suppressed; every real change and every "
      "liveness transition still broadcasts")
