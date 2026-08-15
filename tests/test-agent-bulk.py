"""Tests for POST /api/agents/bulk — one action across many agents.

Partial success is the whole point of the endpoint, so most of these assert
FAILURE handling: that one bad agent does not abort the batch, that each row
carries the status the single-agent route would have returned, and that the
response is still 200 so a client can render "N done, M failed" instead of
being told only that something went wrong.

Driven against the fake stream-json agent (tests/fake_agent.py) — no real
billed Claude session.

Usage: python tests/test-agent-bulk.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ["TRIO_AGENT_CMD"] = f"{sys.executable} {HERE / 'fake_agent.py'}"

_tmp = Path(tempfile.mkdtemp(prefix="nth_bulk_"))
os.environ["NTH_HOME"] = str(_tmp / "home")
(_tmp / "home").mkdir(parents=True, exist_ok=True)

import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


srv.DB_DIR = _tmp
srv.DB_PATH = _tmp / "nth.db"


def http(port, path, method="POST", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except (RemoteDisconnected, ConnectionError) as exc:
        # do_POST has no wrapping handler either: report a dropped connection
        # rather than aborting the run.
        return 0, {"dropped": str(exc)}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def results(d):
    """The per-agent rows, or [] if the batch never produced any.

    Defensive on purpose: if the handler raises, the connection drops and there
    is no `results` key at all. Indexing it directly would abort the run with a
    KeyError and hide every check after this one — the failure would still be
    caught, but it would be reported as the wrong thing in the wrong place.
    """
    return d.get("results", []) if isinstance(d, dict) else []


def row(agent_id):
    db = sqlite3.connect(str(srv.DB_PATH)); db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    finally:
        db.close()


json.loads(srv.nth_connect(summary="t", name="Host", channel="chan-b"))

web.NthWebHandler._default_channel = ""
web.NthWebHandler.db_path = srv.DB_PATH
web._DB_PATH_GLOBAL = srv.DB_PATH
web._SUPERVISOR = None

server = None
try:
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    ids = []
    for _ in range(3):
        st, d = http(port, "/api/agents", body={
            "model": "sonnet", "channels": ["chan-b"], "prompt": "hi"})
        assert st == 200, (st, d)
        ids.append(d["agent"]["id"])
    check("fixture: three agents created", len(set(ids)) == 3)

    # ── happy path ──
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": ids, "action": "stop"})
    check("stop across three agents: 200, all ok, none failed",
          st == 200 and d["ok"] is True and d["count"] == 3
          and d["failed"] == 0 and sorted(d["succeeded"]) == sorted(ids)
          and all(r["ok"] for r in results(d)))
    check("stop actually applied to every agent",
          all(row(a)["state"] == "stopped" for a in ids))

    # ── partial success: an unknown id must not sink the batch ──
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0], "no-such-agent"], "action": "stop"})
    by_id = {r["agent_id"]: r for r in results(d)}
    check("unknown agent: still 200, real agent succeeds, unknown reports 404",
          st == 200 and d.get("failed") == 1
          and by_id[ids[0]]["ok"] is True
          and by_id["no-such-agent"]["ok"] is False
          and by_id["no-such-agent"]["status"] == 404)

    # ── partial success: an AgentActionError mid-batch ──
    # An archived agent rejects lifecycle actions with 409. The agents after it
    # in the list must still be processed — that is what proves the per-agent
    # try/except is inside the loop and not around it.
    st, _ = http(port, f"/api/agents/{ids[1]}/archive")
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0], ids[1], ids[2]], "action": "wake"})
    by_id = {r["agent_id"]: r for r in results(d)}
    check("archived agent in the middle: 409 for it, others still processed",
          st == 200 and len(by_id) == 3 and len(results(d)) == 3
          and by_id.get(ids[1], {}).get("ok") is False and by_id[ids[1]]["status"] == 409
          and by_id[ids[0]]["ok"] is True and by_id[ids[2]]["ok"] is True)
    check("result order matches the requested order",
          [r["agent_id"] for r in results(d)] == [ids[0], ids[1], ids[2]])
    http(port, f"/api/agents/{ids[1]}/unarchive")

    # ── de-dupe ──
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0], ids[0], ids[0]], "action": "stop"})
    check("a repeated id is collapsed to one operation",
          st == 200 and d["count"] == 1 and len(results(d)) == 1)
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[2], ids[0], ids[2]], "action": "stop"})
    check("de-dupe preserves first-seen order",
          [r["agent_id"] for r in results(d)] == [ids[2], ids[0]])

    # ── a body-carrying action applies its params to every agent ──
    # Read the DB back: a 200 only says the request was accepted.
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0], ids[2]], "action": "wake-mode",
        "params": {"mode": "all"}})
    check("wake-mode via bulk: 200 and applied to every agent in the batch",
          st == 200 and d["failed"] == 0
          and row(ids[0])["wake_mode"] == "all"
          and row(ids[2])["wake_mode"] == "all")

    # A validation failure inside the action surfaces as that agent's row with
    # the status the single-agent route would have returned, not as a 4xx for
    # the whole batch.
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0]], "action": "wake-mode",
        "params": {"mode": "nonsense"}})
    check("a per-agent validation failure is reported in its row, batch stays 200",
          st == 200 and d["failed"] == 1
          and results(d) and results(d)[0]["status"] == 400
          and "all, about, or at" in results(d)[0]["error"])

    # ── argument validation ──
    for body, label in (
        ({"agent_ids": ids, "action": "explode"}, "unknown action"),
        ({"agent_ids": ids, "action": ""}, "empty action"),
        ({"agent_ids": ids}, "missing action"),
        ({"action": "stop"}, "missing agent_ids"),
        ({"agent_ids": "not-a-list", "action": "stop"}, "agent_ids not a list"),
        ({"agent_ids": {}, "action": "stop"}, "agent_ids an object"),
        ({"agent_ids": [], "action": "stop"}, "agent_ids empty"),
        ({"agent_ids": ["", "  "], "action": "stop"}, "agent_ids all blank"),
        ({"agent_ids": ids, "action": "stop", "params": "nope"},
         "params not an object"),
        ({"agent_ids": ids, "action": "stop", "params": []}, "params a list"),
    ):
        st, _ = http(port, "/api/agents/bulk", body=body)
        check(f"rejected with 400: {label}", st == 400)

    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": ids, "action": "stop", "params": None})
    check("params omitted (null) is treated as {}, not rejected", st == 200)

    # ── the batch ceiling ──
    # MAX_BULK_AGENTS shipped with the supervisor and had no consumer until
    # now; this is the assertion that gives it one.
    over = [f"ghost-{i}" for i in range(web.MAX_BULK_AGENTS + 1)]
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": over, "action": "stop"})
    check(f"more than MAX_BULK_AGENTS ({web.MAX_BULK_AGENTS}) rejected", st == 400)
    at_limit = [f"ghost-{i}" for i in range(web.MAX_BULK_AGENTS)]
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": at_limit, "action": "stop"})
    check("exactly MAX_BULK_AGENTS is accepted",
          st == 200 and d["count"] == web.MAX_BULK_AGENTS)

    # ── routing ──
    # /api/agents/bulk must not be read as an agent literally named "bulk".
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0]], "action": "stop"})
    check("the bulk route wins over the per-agent action route",
          st == 200 and "results" in d)

    # ── the operator gate, asserted THROUGH the predicate ──
    # A loopback probe resolves to an all-seeing operator, so a request from
    # 127.0.0.1 cannot itself distinguish "the gate works" from "this caller is
    # an operator". Deny through the predicate and confirm nothing was applied.
    http(port, "/api/agents/bulk",
         body={"agent_ids": [ids[0]], "action": "wake-mode",
               "params": {"mode": "at"}})
    before = row(ids[0])["wake_mode"]
    original = web.NthWebHandler._require_operator

    def _deny(self):
        self._error(403, "operator required")
        return None

    web.NthWebHandler._require_operator = _deny
    try:
        st, _ = http(port, "/api/agents/bulk",
                     body={"agent_ids": [ids[0]], "action": "wake-mode",
                           "params": {"mode": "all"}})
        check("a caller the operator gate rejects changes nothing",
              st != 200 and row(ids[0])["wake_mode"] == before)
    finally:
        web.NthWebHandler._require_operator = original
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    try:
        web.get_supervisor().shutdown()
    except Exception:
        pass
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("OK — all bulk-action checks passed")
