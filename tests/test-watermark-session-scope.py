"""The monitor MUST consult sessions.last_read while ack is session-scoped.

This is a dependency guard, not a feature test.

The downstream fork drops `sessions.last_read` from the monitor's watermark
reconciliation, on the grounds that the session capability became agent-GLOBAL
there and the column is therefore meaningless. That change was slated to ride
along with global identity — but it cannot, because the migration it depends on
has not happened here.

On this branch `trio_ack` is strictly either/or (nth_server.py, `nth_ack`):

    if sess is not None:  UPDATE sessions SET last_read = ...
    else:                 UPDATE members  SET last_read = ...

A session-token client's ack therefore NEVER touches `members.last_read`. If
the monitor stopped reading `sessions.last_read`, every message that client
already acknowledged would be re-notified on the next tick — silently, and at a
cost that scales with messages × agents, which is precisely the failure this
project has been bitten by before.

So this test pins the BEHAVIOUR that makes the monitor's read necessary. It
deliberately does not try to police the monitor's source — see the note in the
body about why a source-grep cannot express "this code must still run".

Usage: python tests/test-watermark-session-scope.py
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

_tmp = Path(tempfile.mkdtemp(prefix="nth_hwm_"))
os.environ["NTH_HOME"] = str(_tmp)

import nth_server as srv    # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


srv.DB_DIR = _tmp
srv.DB_PATH = _tmp / "nth.db"

try:
    # ── the behaviour: an ack through a session token leaves members alone ──
    joined = json.loads(srv.nth_connect(summary="x", name="Ada", channel="chan-w"))
    mid, token = joined["member_id"], joined["session_token"]
    peer = json.loads(srv.nth_connect(summary="x", name="Bob", channel="chan-w"))
    for i in range(3):
        srv.nth_send(member_id=peer["member_id"], channel="chan-w",
                     message=f"message {i}",
                     session_token=peer["session_token"])

    conn = sqlite3.connect(str(srv.DB_PATH))
    top = conn.execute(
        "SELECT MAX(id) FROM messages WHERE channel='chan-w'").fetchone()[0]
    conn.close()

    acked = json.loads(srv.nth_ack(member_id=mid, channel="chan-w",
                                   through_id=top, session_token=token))
    check("fixture: the session-token ack succeeded",
          acked.get("ok") and acked.get("watermark") == top)

    conn = sqlite3.connect(str(srv.DB_PATH))
    conn.row_factory = sqlite3.Row
    member_hwm = conn.execute(
        "SELECT last_read FROM members WHERE id=? AND channel='chan-w'",
        (mid,)).fetchone()["last_read"]
    session_hwm = conn.execute(
        "SELECT last_read FROM sessions WHERE session_token=?",
        (token,)).fetchone()["last_read"]
    conn.close()

    check("the ack advanced sessions.last_read", session_hwm == top)
    # members.last_read is NOT zero here — connect initialises it past the
    # join message — so the assertion is that it LAGS, not that it is unset.
    check("and did NOT advance members.last_read, which still lags behind — "
          "so members alone is not a sufficient watermark for a session-token "
          f"client (members={member_hwm}, sessions={session_hwm})",
          member_hwm < session_hwm and member_hwm != top)

    # NOTE ON SCOPE. An earlier version of this file also grepped
    # nth_monitor.py's AST for a SQL string containing "FROM sessions" and
    # "last_read", claiming to guard the monitor. It did not: commenting out
    # the CALL while leaving the literal, or moving the SQL into a docstring,
    # both slip past, while rewriting the query as an f-string breaks it for no
    # reason. A source-grep cannot express "this code must still run", and the
    # change it was guarding against happens in a different repo whose author
    # would simply delete this file.
    #
    # What is left is the behavioural fact, which is what actually matters and
    # is what any future author needs to see: a session-token ack advances ONE
    # of the two watermarks. The monitor must reconcile both for as long as
    # that is true. The corresponding note lives at the monitor's call site.
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    print("\nIf you are intentionally making sessions agent-global, make ack "
          "write members.last_read BEFORE the monitor stops reading "
          "sessions.last_read — this test and that read retire together.")
    sys.exit(1)
print("OK — a session-token ack advances only the session watermark")
