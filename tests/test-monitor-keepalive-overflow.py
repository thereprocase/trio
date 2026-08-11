"""Regression test: keepalive emit must not crash for a never-engaged member.

Reproduces the OverflowError crash-loop documented in
BUGREPORT-monitor-overflow.md: when a member has recent own-activity but was
never sigil-engaged, seconds_since(None) returns float("inf"), and the old
emit code did round(inf) -> OverflowError, killing the monitor process.

The fix routes the diagnostic gap field through gap_for_emit(), which maps
inf -> None ("never engaged") and otherwise rounds. This test drives the REAL
module functions (seconds_since, gap_for_emit, emit) rather than duplicated
logic, and asserts the emitted event is JSON-serializable.

Usage: python test-monitor-keepalive-overflow.py
"""
import json
import sys
from pathlib import Path

# Import the real module under test from ../server.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import nth_monitor as nm


failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


# 1. The inf sentinel originates from the real seconds_since(None) — the exact
#    state of a member who was never sigil-engaged (last_engaged row is None).
engaged_gap = nm.seconds_since(None)
check("seconds_since(None) is inf sentinel", engaged_gap == float("inf"))

# 2. The old code path genuinely raised — confirm the regression is real so a
#    future refactor that reintroduces round(inf) is caught here.
raised = False
try:
    round(engaged_gap)
except OverflowError:
    raised = True
check("round(inf) still raises OverflowError (regression guard)", raised)

# 3. The fix: gap_for_emit maps inf -> None and rounds finite values.
check("gap_for_emit(inf) is None", nm.gap_for_emit(engaged_gap) is None)
check("gap_for_emit(15006.7) == 15007", nm.gap_for_emit(15006.7) == 15007)
check("gap_for_emit(0.0) == 0", nm.gap_for_emit(0.0) == 0)

# 4. End-to-end: drive the REAL production event constructor that monitor()
#    calls — build_keepalive_event() — so this test protects the actual emit
#    path. A revert of either gap field to round() would raise here on inf.
#    own_gap is always finite for a connected member (it has a heartbeat).
own_gap = 15006.0
event = nm.build_keepalive_event(own_gap, engaged_gap)
check("build_keepalive_event tags event 'keepalive'", event["event"] == "keepalive")
check("build_keepalive_event routes engaged_gap through the inf guard",
      event["engaged_gap_seconds"] is None)
serialized = None
try:
    serialized = json.dumps(event)
except (OverflowError, ValueError, TypeError) as e:
    check(f"keepalive event serializes (raised {e!r})", False)
if serialized is not None:
    payload = json.loads(serialized)
    check("keepalive event serializes cleanly", True)
    check("engaged_gap_seconds serializes as null", payload["engaged_gap_seconds"] is None)
    check("gap_seconds serializes as rounded int", payload["gap_seconds"] == 15006)

if failures:
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("\nAll checks passed.")
