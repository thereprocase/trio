#!/usr/bin/env python3
"""Buddy-icon uniqueness is a database constraint, not a convention.

Both writers allocate inside BEGIN IMMEDIATE, which is what makes concurrent
selection safe. That is correct and it is not enough: it leaves the invariant
defended only by two call sites remembering to do it. A third writer added
later, a manual edit, or a restore breaks it silently -- and nothing notices,
because there is no alarm attached. The face pile's entire job is telling
agents apart, so two identical portraits is a user-visible correctness failure.

This pins the constraint itself, and the two properties that make it safe to
add to a database that already exists:

  * archived agents keep their portrait so unarchiving can restore it, so the
    index must be PARTIAL or unarchiving would collide with the living
  * '' is the legitimate not-yet-assigned value and many agents share it
  * a database that ALREADY contains duplicates must still open. The schema
    runs on every connection, so a hard failure would raise at import and the
    hub would not boot -- the constraint must degrade to today's behaviour
    rather than to a dead service.

Usage: python3 tests/test-avatar-uniqueness.py
"""
import os
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

failures = []
passed = 0


def check(name, cond):
    global passed
    if cond:
        passed += 1
        print("PASS: " + name)
    else:
        failures.append(name)
        print("FAIL: " + name)


try:
    import nth_server
except ImportError as exc:  # pragma: no cover - reported by run-all.sh
    print(f"No module named 'mcp'" if "mcp" in str(exc) else str(exc))
    raise

ACTIVE = "INSERT INTO agents (id, name, model, base_prompt, state, managed, avatar_name, created_at) VALUES (?,?,'','','idle',1,?,'2026-01-01')"


def fresh_db(path):
    # get_db() resolves DB_DIR/DB_PATH as module globals at call time, so those
    # are what has to be redirected. An env var does nothing here — an earlier
    # draft of this file set NTH_DB, silently ran against the developer's real
    # ~/.claude/nth/nth.db, and wrote fixture agents into it. Redirect the
    # globals, and assert the redirect took before touching anything.
    nth_server.DB_DIR = Path(path).parent
    nth_server.DB_PATH = Path(path)
    db = nth_server.get_db()
    actual = db.execute("PRAGMA database_list").fetchall()[0][2]
    assert os.path.realpath(actual) == os.path.realpath(path), (
        f"test would have run against {actual}, not the temp database")
    return db


with tempfile.TemporaryDirectory() as tmp:
    db_path = os.path.join(tmp, "clean.db")
    db = fresh_db(db_path)

    # ── the index exists at all ──────────────────────────────────────────────
    idx = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_agents_avatar_active'").fetchone()
    check("a uniqueness index exists for active buddy icons", idx is not None)

    # ── it actually refuses a duplicate ──────────────────────────────────────
    db.execute(ACTIVE, ("a1", "A", "Luna"))
    db.commit()
    duplicate_refused = False
    try:
        db.execute(ACTIVE, ("a2", "B", "Luna"))
        db.commit()
    except sqlite3.IntegrityError:
        duplicate_refused = True
    check("two ACTIVE agents cannot hold the same buddy icon", duplicate_refused)

    # ── the two values that must NOT collide ─────────────────────────────────
    # Archived agents keep their portrait so unarchiving can restore it. If the
    # index were not partial, every archived agent would block its own return.
    # Guarded so a non-partial index FAILS this check rather than aborting the
    # run: an uncaught IntegrityError here would kill every assertion below it,
    # and a mutation that kills the process looks the same as one that is caught
    # while telling you far less.
    archived_allowed = True
    try:
        db.execute("INSERT INTO agents (id, name, model, base_prompt, state,"
                   " managed, avatar_name, created_at, archived_at) VALUES"
                   " ('a3','C','','','idle',1,'Luna','2026-01-01','2026-01-02')")
        db.commit()
    except sqlite3.IntegrityError:
        archived_allowed = False
    check("an ARCHIVED agent may hold a portrait an active agent is using",
          archived_allowed
          and db.execute("SELECT COUNT(*) FROM agents WHERE avatar_name='Luna'"
                         ).fetchone()[0] == 2)

    # '' is the not-yet-assigned value and is shared by everyone unassigned.
    db.execute(ACTIVE, ("a4", "D", ""))
    db.execute(ACTIVE, ("a5", "E", ""))
    db.commit()
    check("many agents may share the empty not-yet-assigned value",
          db.execute("SELECT COUNT(*) FROM agents WHERE avatar_name=''"
                     ).fetchone()[0] == 2)

    # A distinct portrait is still fine.
    db.execute(ACTIVE, ("a6", "F", "Atlas"))
    db.commit()
    check("a distinct portrait is accepted", True)
    db.close()

    # ── a database that already has duplicates must still OPEN ───────────────
    # This is the property that keeps the constraint from being a footgun: the
    # schema runs on every connection, so raising here would mean the hub could
    # not start against a database predating the rule.
    # Built by letting the real schema create itself, then dropping the index
    # and seeding duplicates — which is exactly the shape of a database that
    # predates the constraint. Hand-rolling a minimal `agents` table instead
    # tests nothing useful: the other additive migrations fail first on the
    # missing columns, so the open would break for a reason that has nothing to
    # do with the property under test.
    dirty_path = os.path.join(tmp, "dirty.db")
    seed = fresh_db(dirty_path)
    seed.execute("DROP INDEX IF EXISTS idx_agents_avatar_active")
    seed.execute(ACTIVE, ("d1", "X", "Luna"))
    seed.execute(ACTIVE, ("d2", "Y", "Luna"))
    seed.commit()
    seed.close()

    opened = True
    try:
        dirty = fresh_db(dirty_path)
    except Exception as exc:
        opened = False
        print(f"    opening the dirty database raised: {exc}")
    check("a database with pre-existing duplicates still opens", opened)

    if opened:
        idx = dirty.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_agents_avatar_active'").fetchone()
        check("...and simply has no index, rather than a half-applied one",
              idx is None)
        check("...with both duplicate rows left intact rather than deleted",
              dirty.execute("SELECT COUNT(*) FROM agents WHERE avatar_name='Luna'"
                            ).fetchone()[0] == 2)
        dirty.close()

    # ── the backstop path inside the tool ────────────────────────────────────
    # The checks above prove the index exists and refuses duplicates. They say
    # nothing about what nth_set_avatar DOES when it fires — and the whole point
    # of the handler is that a constraint violation should read as the same
    # honest refusal the pre-check gives, not as an exception escaping the tool.
    # Forced here by making the UPDATE raise, because the pre-check makes the
    # real path unreachable by design.
    tool_path = os.path.join(tmp, "tool.db")
    tool_db = fresh_db(tool_path)
    tool_db.close()

    class RaisingConn:
        """Real connection, except UPDATE agents raises IntegrityError."""
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if sql.lstrip().upper().startswith("UPDATE AGENTS"):
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: agents.avatar_name")
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_get_db = nth_server.get_db
    seeded = real_get_db()
    seeded.execute(ACTIVE, ("t1", "Tester", ""))
    seeded.execute(
        "INSERT INTO channels (code, status, created_at, updated_at) VALUES"
        " ('room','active','2026-01-01','2026-01-01')")
    seeded.execute(
        "INSERT INTO members (id, channel, name, summary, skills, joined_at,"
        " last_seen, active) VALUES"
        " ('t1','room','Tester','','','2026-01-01','2026-01-01',1)")
    seeded.execute(
        "INSERT INTO sessions (session_token, member_id, channel, role,"
        " connected_at, last_seen) VALUES"
        " ('tok','t1','room','primary','2026-01-01','2026-01-01')")
    seeded.commit()
    seeded.close()

    nth_server.get_db = lambda: RaisingConn(real_get_db())
    try:
        import json as _json
        result = _json.loads(
            nth_server.nth_set_avatar("room", "t1", "Luna", session_token="tok"))
    except sqlite3.IntegrityError:
        result = {"__raised__": True}
    finally:
        nth_server.get_db = real_get_db

    check("a constraint violation is translated, not raised out of the tool",
          "__raised__" not in result)
    check("...into the same honest refusal the pre-check gives",
          "already in use" in (result.get("error") or ""))

    # ── the dirty database warns once, not per request ───────────────────────
    # get_db() runs per request; a warning on every open would write this line
    # thousands of times a day against a database an operator already knows is
    # dirty.
    import io
    import contextlib
    nth_server._AVATAR_INDEX_WARNED = False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        for _ in range(3):
            fresh_db(dirty_path).close()
    check("a dirty database warns exactly once per process, not per open",
          err.getvalue().count("buddy-icon uniqueness index not created") == 1)

    # ── the two web backstops ────────────────────────────────────────────────
    # These CANNOT be driven to the constraint honestly: pick_agent_avatar
    # returns '' when every portrait is taken (nth_web.py:2813), and '' is a
    # value the partial index permits, so no amount of saturation produces a
    # collision. The translations are defensive handling for a state the
    # allocator will not create — the same status as the MCP one above, and the
    # same reason it was still worth adding: an unreachable path should refuse
    # cleanly rather than return a raw 500 or drop the socket.
    #
    # So the allocator is replaced, deliberately and narrowly, to return a
    # portrait an active fixture already holds. That tests the handler, which
    # is the thing under test, without pretending the state is reachable.
    import threading
    import time
    import urllib.error
    import urllib.request

    os.environ["TRIO_AGENT_CMD"] = (
        f"{sys.executable} {os.path.join(os.path.dirname(__file__), 'fake_agent.py')}")
    import nth_web as web

    web_path = os.path.join(tmp, "web.db")
    fresh_db(web_path).close()
    channel = json.loads(nth_server.nth_connect(
        summary="t", name="Host", channel="avtest"))["channel"]

    fixture = nth_server.get_db()
    fixture.execute(ACTIVE, ("live", "Live", "Luna"))
    fixture.execute("INSERT INTO agents (id, name, model, base_prompt, state,"
                    " managed, avatar_name, created_at, archived_at) VALUES"
                    " ('arch','Arch','','','stopped',1,'','2026-01-01','2026-01-02')")
    fixture.commit()
    fixture.close()

    def post(port, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}
        except Exception as exc:
            # The unarchive translation exists to stop the socket closing, so
            # that outcome gets its own name rather than looking like any error.
            return "socket-dropped", {"exc": str(exc)}

    hub = web.EventHub(nth_server.DB_PATH, channel)
    real_pick = web.pick_agent_avatar
    server = None
    try:
        hub.start()
        web.NthWebHandler.hub = hub
        web.NthWebHandler.channel = channel
        web.NthWebHandler.db_path = nth_server.DB_PATH
        web._DB_PATH_GLOBAL = nth_server.DB_PATH
        server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
        server.daemon_threads = True
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.3)

        web.pick_agent_avatar = lambda *a, **k: "Luna"   # already held by 'live'

        before = nth_server.get_db()
        n_before = before.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        before.close()

        status, body = post(port, "/api/agents",
                            {"name": "Overflow", "channels": [channel]})
        check("managed create answers 409, not a raw 500", status == 409)
        check("...naming the buddy-icon conflict", "buddy icon" in str(body).lower())
        after = nth_server.get_db()
        n_after = after.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        after.close()
        check("...and leaves no durable half-created agent row", n_after == n_before)

        status, body = post(port, "/api/agents/arch/unarchive", {})
        check("unarchive answers 409 rather than dropping the socket",
              status == 409)
        check("...and the archived row is left archived, not half-restored",
              (lambda d: d.execute(
                  "SELECT archived_at FROM agents WHERE id='arch'"
              ).fetchone()[0] is not None)(nth_server.get_db()))
    finally:
        web.pick_agent_avatar = real_pick
        if server is not None:
            server.shutdown()
            server.server_close()
        try:
            hub.stop()
        except Exception:
            pass

print()
if failures:
    print(f"FAILED — {len(failures)} of {len(failures) + passed}")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print(f"OK — {passed} passed")
