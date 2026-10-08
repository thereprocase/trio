#!/usr/bin/env python3
"""Regression test: a waiting long-poll is cheap and wakes promptly.

Covers:
  1. A send in this process wakes a waiting poll at once (faster than any
     re-check could).
  2. A message written by another connection (as nth_web writes a dashboard
     post) wakes a waiting poll within the re-check interval.
  3. A quiet poll runs a pass at its start and one at its deadline, and
     none in between.
  4. The heartbeat is written at most once per POLL_HEARTBEAT_SECONDS.
  5. Traffic in another channel wakes no full pass; a forced ack that moves
     the watermark back does.
  6. get_db() runs the schema once per database file, again after a schema
     change by another connection or for a replaced file, and again after a
     run that could not finish (a lock timeout during a migration).

Run: python3 tests/test-poll-wait.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["NTH_QUIET"] = "1"
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import nth_server

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def join(channel, name):
    data = json.loads(nth_server.nth_connect(summary=name, name=name, channel=channel))
    return data["member_id"], data["session_token"]


def poll_in_thread(channel, member_id, token, wait):
    box = {}

    def run():
        started = time.monotonic()
        box["result"] = json.loads(nth_server.nth_poll(
            channel=channel, member_id=member_id, session_token=token, wait_seconds=wait))
        box["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


with tempfile.TemporaryDirectory() as tmp:
    nth_server.DB_DIR = Path(tmp)
    nth_server.DB_PATH = Path(tmp) / "nth.db"
    channel = "poll-wait"
    reader, reader_token = join(channel, "reader")
    writer, writer_token = join(channel, "writer")
    # Start the reader from the current end of the channel.
    first = json.loads(nth_server.nth_poll(channel=channel, member_id=reader,
                                           session_token=reader_token, wait_seconds=0))
    if first.get("messages"):
        nth_server.nth_ack(channel=channel, member_id=reader, session_token=reader_token,
                           through_id=first["messages"][-1]["id"])

    # 1. In-process send wakes the poll at once. The re-check is pushed out to
    # 5 s so only the notify path can answer within the bound below.
    nth_server.POLL_RECHECK_SECONDS = 5.0
    thread, box = poll_in_thread(channel, reader, reader_token, 10)
    time.sleep(0.5)
    nth_server.nth_send(channel=channel, member_id=writer, message="hello",
                        session_token=writer_token)
    thread.join(12)
    nth_server.POLL_RECHECK_SECONDS = 1.0
    check("in-process send wakes a waiting poll",
          box.get("result", {}).get("event") == "new_messages", box)
    check("in-process wake comes from the notify, before any re-check",
          box.get("elapsed", 99) < 2.0, box.get("elapsed"))
    nth_server.nth_ack(channel=channel, member_id=reader, session_token=reader_token,
                       through_id=box["result"]["messages"][-1]["id"])

    # 2. Another connection's write wakes the poll within the re-check interval.
    thread, box = poll_in_thread(channel, reader, reader_token, 10)
    time.sleep(0.5)
    outside = sqlite3.connect(str(nth_server.DB_PATH))
    outside.execute(
        "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (channel, writer, "writer", "from the dashboard",
                                   nth_server.now_iso()))
    outside.commit()
    outside.close()
    thread.join(12)
    check("another connection's write wakes a waiting poll",
          box.get("result", {}).get("event") == "new_messages", box)
    check("cross-connection wake within re-check interval plus margin",
          box.get("elapsed", 99) < 0.5 + nth_server.POLL_RECHECK_SECONDS + 1.0,
          box.get("elapsed"))
    nth_server.nth_ack(channel=channel, member_id=reader, session_token=reader_token,
                       through_id=box["result"]["messages"][-1]["id"])

    # 3 + 4. A quiet poll: one full pass, one heartbeat write.
    passes = []
    real_get_member = nth_server._get_member

    def counting_get_member(db, ch, mid):
        if mid == reader:
            passes.append(time.monotonic())
        return real_get_member(db, ch, mid)

    nth_server._get_member = counting_get_member
    beats = []
    real_beat = nth_server._poll_heartbeat

    def counting_beat(*args, **kwargs):
        beats.append(time.monotonic())
        return real_beat(*args, **kwargs)

    nth_server._poll_heartbeat = counting_beat
    started = time.monotonic()
    quiet = json.loads(nth_server.nth_poll(channel=channel, member_id=reader,
                                           session_token=reader_token, wait_seconds=5))
    elapsed = time.monotonic() - started
    check("quiet poll waits out its deadline", quiet.get("event") == "no_new" and elapsed >= 4.5,
          (quiet.get("event"), elapsed))
    # One pass at the start and one at the deadline; the old loop ran one every 2 s.
    check("quiet poll runs two full passes (was one every 2 s)", len(passes) == 2, len(passes))
    check("quiet poll writes the heartbeat once", len(beats) == 1, len(beats))

    # 4b. With a short throttle the deadline pass writes it again.
    nth_server.POLL_HEARTBEAT_SECONDS = 1.5
    beats.clear()
    nth_server.nth_poll(channel=channel, member_id=reader, session_token=reader_token,
                        wait_seconds=3)
    nth_server.POLL_HEARTBEAT_SECONDS = 20.0
    check("heartbeat is written again once the throttle has passed", len(beats) == 2, len(beats))

    # 5. Another channel's traffic, from this process and from another
    # connection, wakes no full pass in this one.
    other, other_token = join("poll-wait-other", "other")
    passes.clear()
    thread, box = poll_in_thread(channel, reader, reader_token, 5)
    for i in range(6):
        time.sleep(0.5)
        nth_server.nth_send(channel="poll-wait-other", member_id=other,
                            message=f"elsewhere {i}", session_token=other_token)
        outside = sqlite3.connect(str(nth_server.DB_PATH))
        outside.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)", ("poll-wait-other", other, "other", "dashboard",
                                       nth_server.now_iso()))
        outside.commit()
        outside.close()
    thread.join(10)
    check("other channels' traffic wakes no full pass", len(passes) == 2, len(passes))
    nth_server._get_member = real_get_member
    nth_server._poll_heartbeat = real_beat

    # 5b. A forced ack that moves the watermark back wakes a waiting poll
    # (old messages are unread again).
    thread, box = poll_in_thread(channel, reader, reader_token, 10)
    time.sleep(0.5)
    nth_server.nth_ack(channel=channel, member_id=reader, session_token=reader_token,
                       through_id=0, force=True)
    thread.join(12)
    check("a forced ack back to 0 wakes a waiting poll with the re-opened messages",
          box.get("result", {}).get("event") == "new_messages"
          and box.get("elapsed", 99) < 3.0, (box.get("result", {}).get("event"),
                                              box.get("elapsed")))
    last = box["result"]["messages"][-1]["id"]
    nth_server.nth_ack(channel=channel, member_id=reader, session_token=reader_token,
                       through_id=last)

    # 6. Schema runs once per file, again after another connection changes the
    # schema, and again for a replaced file.
    statements = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    nth_server.sqlite3.connect = tracing_connect
    try:
        nth_server.get_db().close()
        creates = [s for s in statements if "CREATE TABLE" in s]
        check("get_db skips the schema for a ready file", creates == [], len(creates))
        outside = real_connect(str(nth_server.DB_PATH))
        outside.execute("ALTER TABLE members ADD COLUMN test_only_column TEXT")
        outside.commit()
        outside.close()
        statements.clear()
        nth_server.get_db().close()
        creates = [s for s in statements if "CREATE TABLE" in s]
        check("get_db reruns the schema after another connection changes it", creates != [],
              len(creates))
        nth_server.DB_PATH.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(nth_server.DB_PATH) + suffix).unlink(missing_ok=True)
        statements.clear()
        db = nth_server.get_db()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.close()
        check("get_db rebuilds the schema for a replaced file",
              {"channels", "members", "messages", "sessions"} <= tables, sorted(tables))

        # 6b. A legacy tool_events rebuild that times out on a lock is not
        # cached as done: the next open finishes it.
        legacy = real_connect(str(nth_server.DB_PATH))
        legacy.execute("DROP TABLE tool_events")
        legacy.execute(
            "CREATE TABLE tool_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "tool_name TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', "
            "created_at TEXT NOT NULL)")
        legacy.execute("INSERT INTO tool_events (session_id, created_at) VALUES ('fp', 'x')")
        legacy.commit()
        legacy.close()

        def hold_write_lock():
            holder = real_connect(str(nth_server.DB_PATH), isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
            time.sleep(7)
            holder.execute("COMMIT")
            holder.close()

        locker = threading.Thread(target=hold_write_lock)
        locker.start()
        time.sleep(0.3)
        try:
            nth_server.get_db().close()
        except sqlite3.Error:
            pass  # the open may fail outright under the lock; either way it must not be cached
        locker.join()

        def tool_event_columns():
            probe = real_connect(str(nth_server.DB_PATH))
            cols = {r[1] for r in probe.execute("PRAGMA table_info(tool_events)")}
            probe.close()
            return cols

        check("a rebuild that timed out on a lock is still pending",
              "fingerprint" not in tool_event_columns(), sorted(tool_event_columns()))
        nth_server.get_db().close()
        check("the next open finishes the tool_events rebuild",
              "fingerprint" in tool_event_columns(), sorted(tool_event_columns()))
    finally:
        nth_server.sqlite3.connect = real_connect

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
