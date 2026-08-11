#!/usr/bin/env python3
"""Regression test: v7.3 nodes table + fleet check-ins.

Covers:
  1. get_db() creates the nodes table.
  2. upsert_node is idempotent per (hostname, transport) — re-checkin
     updates last_seen/pid instead of duplicating rows.
  3. nth_connect records the server's own node row, and a declared
     node_host from a different machine lands as a 'spoke' row.
  4. _checkin_self_node never raises when the nodes table is missing
     (pre-v7.3 DB touched by an old server).

Run: python3 tests/test-nodes-upsert.py
"""
import os
import sys
import socket
import sqlite3
import tempfile
from pathlib import Path

os.environ["NTH_QUIET"] = "1"
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import nth_server

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


with tempfile.TemporaryDirectory() as tmp:
    nth_server.DB_DIR = Path(tmp)
    nth_server.DB_PATH = Path(tmp) / "nth.db"

    # 1. Schema
    db = nth_server.get_db()
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("nodes table created", "nodes" in tables)

    # 2. Idempotent upsert
    nth_server.upsert_node(db, "boxa", "stdio", nth_version="7.3.0", pid=111)
    nth_server.upsert_node(db, "boxa", "stdio", nth_version="7.3.0", pid=222)
    nth_server.upsert_node(db, "boxa", "monitor", nth_version="7.3.0", pid=333)
    db.commit()
    rows = db.execute(
        "SELECT * FROM nodes WHERE hostname='boxa' ORDER BY transport").fetchall()
    check("one row per (host, transport)", len(rows) == 2)
    stdio_row = [r for r in rows if r["transport"] == "stdio"][0]
    check("upsert updates pid in place", stdio_row["pid"] == 222)
    check("python version recorded", stdio_row["python"].count(".") == 2)
    db.close()

    # 3. connect writes self row + declared spoke row
    out = nth_server.nth_connect(
        summary="node test", name="Tester", channel="node-test",
        node_host="faraway-spoke", node_version="7.2.0",
    )
    check("connect succeeds", '"ok": true' in out)
    db = nth_server.get_db()
    self_row = db.execute(
        "SELECT * FROM nodes WHERE hostname = ? AND transport = ?",
        (socket.gethostname(), nth_server.NODE_TRANSPORT)).fetchone()
    check("connect records own process row", self_row is not None)
    check("own row carries NTH_VERSION",
          self_row is not None and self_row["nth_version"] == nth_server.NTH_VERSION)
    spoke_row = db.execute(
        "SELECT * FROM nodes WHERE hostname = 'faraway-spoke'").fetchone()
    check("declared node_host lands as spoke row",
          spoke_row is not None and spoke_row["transport"] == "spoke")
    check("spoke row carries declared version",
          spoke_row is not None and spoke_row["nth_version"] == "7.2.0")

    # Same-host declaration must NOT create a bogus spoke row
    nth_server.nth_connect(
        summary="local", name="Local", channel="node-test",
        node_host=socket.gethostname(),
    )
    local_spoke = db.execute(
        "SELECT * FROM nodes WHERE hostname = ? AND transport = 'spoke'",
        (socket.gethostname(),)).fetchone()
    check("own hostname never becomes a spoke row", local_spoke is None)
    db.close()

    # 4. Missing-table resilience (pre-v7.3 DB)
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    try:
        nth_server._checkin_self_node(bare, force=True)
        check("_checkin_self_node survives missing nodes table", True)
    except Exception:
        check("_checkin_self_node survives missing nodes table", False)
    bare.close()

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
