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
    check("stop across three agents: 200, ok, every row succeeded",
          st == 200 and d["ok"] is True and d["count"] == 3
          and all(r["ok"] for r in results(d))
          and sorted(r["agent_id"] for r in results(d)) == sorted(ids))
    check("stop actually applied to every agent",
          all(row(a)["state"] == "stopped" for a in ids))

    # ── partial success: an unknown id must not sink the batch ──
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0], "no-such-agent"], "action": "stop"})
    by_id = {r["agent_id"]: r for r in results(d)}
    check("unknown agent: still 200, real agent succeeds, unknown reports 404",
          st == 200 and d["ok"] is False
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
          st == 200 and d["ok"] is True
          and row(ids[0])["wake_mode"] == "all"
          and row(ids[2])["wake_mode"] == "all")

    # ── THE assertion for a bulk route: only the named agents are touched ──
    # Without this a handler that ignores agent_ids entirely and applies the
    # action to every row in `agents` — while still emitting one result row per
    # requested id — passes every other check in this file. "Acted on the wrong
    # set" is the one failure mode a bulk endpoint introduces that the shared
    # single-agent applier does not already guard.
    for a in ids:
        http(port, f"/api/agents/{a}/wake-mode", body={"mode": "at"})
    check("fixture: all three agents start at wake_mode 'at'",
          all(row(a)["wake_mode"] == "at" for a in ids))
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0], ids[2]], "action": "wake-mode",
        "params": {"mode": "all"}})
    check("only the named agents are modified; the unnamed one is untouched",
          st == 200 and row(ids[0])["wake_mode"] == "all"
          and row(ids[2])["wake_mode"] == "all"
          and row(ids[1])["wake_mode"] == "at")
    # Same guard on a lifecycle action, where the collateral damage is worse.
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0]], "action": "archive"})
    check("archive touches only its own agent",
          st == 200 and row(ids[0])["archived_at"] is not None
          and row(ids[1])["archived_at"] is None
          and row(ids[2])["archived_at"] is None)
    http(port, f"/api/agents/{ids[0]}/unarchive")

    # ── "already in that state" is NOT "agent not found" ──
    # The applier returns a falsy ok for both, so reporting 404 for both made
    # select-all -> wake on a healthy roster report every row as
    # "agent not found" — wrong for 100% of rows, in the endpoint's headline
    # use case, and futile to retry.
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0]], "action": "wake"})
    check("fixture: the agent is awake", st == 200)
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0], "ghost-x"], "action": "wake"})
    by_id = {r["agent_id"]: r for r in results(d)}
    check("an already-awake agent reports 409, an unknown id reports 404 — "
          "and the two are distinguishable",
          by_id.get(ids[0], {}).get("status") == 409
          and "already" in by_id.get(ids[0], {}).get("error", "")
          and by_id.get("ghost-x", {}).get("status") == 404
          and by_id.get("ghost-x", {}).get("error") == "agent not found")

    # ── compact must not silently resurrect a stopped agent ──
    # Through the single-agent route the implicit wake is one deliberate click.
    # Across a roster it would restart and bill every sleeping agent.
    http(port, "/api/agents/bulk", body={"agent_ids": ids, "action": "stop"})
    time.sleep(0.2)
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": ids, "action": "compact", "params": {"message": "tidy"}})
    check("bulk compact refuses a stopped agent instead of waking it",
          st in (200, 409) and results(d)
          # .get, not []: a success row carries no `status`, and indexing it
          # would abort the run instead of reporting this check as failed.
          and all(r.get("status") == 409 for r in results(d))
          and all(not web.get_supervisor().is_running(a) for a in ids))

    # A validation failure inside the action surfaces as that agent's row with
    # the status the single-agent route would have returned, not as a 4xx for
    # the whole batch.
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0]], "action": "wake-mode",
        "params": {"mode": "nonsense"}})
    check("a per-agent validation failure is reported in its row",
          d["ok"] is False
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
    # Accepted by the ceiling — and then answered 404, because NOTHING
    # succeeded and every failure was the same client-side status. "You named
    # a hundred agents and none exist" is a bad request, not a partial success,
    # and a script with no JSON parser needs to be able to see that.
    check("exactly MAX_BULK_AGENTS is accepted by the ceiling",
          d["count"] == web.MAX_BULK_AGENTS)
    check("a batch where nothing succeeded and all failures share one 4xx "
          "returns that status, not 200",
          st == 404 and d["ok"] is False)
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0], "ghost-1"], "action": "stop"})
    check("but a batch with even one success stays 200", st == 200)

    # ── the process-spawning ceiling ──
    # The loop is synchronous and spawn() blocks up to 10s per agent, so 100
    # wakes is ~17 minutes inside one HTTP request — past every browser and
    # proxy timeout, while the loop keeps starting processes.
    many = [f"ghost-{i}" for i in range(web.MAX_BULK_SPAWNING_AGENTS + 1)]
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": many, "action": "wake"})
    check(f"more than MAX_BULK_SPAWNING_AGENTS "
          f"({web.MAX_BULK_SPAWNING_AGENTS}) rejected for a spawning action",
          st == 400 and "synchronous" in str(d.get("error", "")))
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": many, "action": "stop"})
    check("the same count is fine for a cheap action", st != 400)

    # ── routing ──
    # /api/agents/bulk must not be read as an agent literally named "bulk".
    st, d = http(port, "/api/agents/bulk",
                 body={"agent_ids": [ids[0]], "action": "stop"})
    check("the bulk route wins over the per-agent action route",
          st == 200 and "results" in d)

    # ── the deliberate broad `except`, which had no test at all ──
    # It exists so one agent in an unexpected state cannot sink the batch.
    # Delete it and the rest of this file stays green, so it needs its own.
    real_apply = web.NthWebHandler._apply_agent_action
    victim = ids[1]

    def _explode_one(self, agent_id, action, params, ident):
        if agent_id == victim:
            raise RuntimeError("provider transport died")
        return real_apply(self, agent_id, action, params, ident)

    web.NthWebHandler._apply_agent_action = _explode_one
    try:
        st, d = http(port, "/api/agents/bulk", body={
            "agent_ids": ids, "action": "wake-mode", "params": {"mode": "about"}})
        by_id = {r["agent_id"]: r for r in results(d)}
        check("an unexpected exception is confined to its own row",
              st == 200 and by_id.get(victim, {}).get("status") == 500
              and "provider transport died" in by_id.get(victim, {}).get("error", ""))
        check("its neighbours on both sides are still applied",
              by_id.get(ids[0], {}).get("ok") is True
              and by_id.get(ids[2], {}).get("ok") is True
              and row(ids[0])["wake_mode"] == "about"
              and row(ids[2])["wake_mode"] == "about")
        check("and the failing agent was genuinely not modified",
              row(victim)["wake_mode"] != "about")
    finally:
        web.NthWebHandler._apply_agent_action = real_apply

    # ── a systemic failure is not N independent agent failures ──
    # A locked database or a supervisor shutting down fails identically for
    # every agent, and each one pays the same timeout to rediscover it.
    def _explode_all(self, agent_id, action, params, ident):
        raise RuntimeError("database is locked")

    web.NthWebHandler._apply_agent_action = _explode_all
    # The batch must be longer than the streak threshold, or there is nothing
    # left to skip and aborting would be meaningless.
    batch = ids + [f"more-{i}" for i in range(web.BULK_SYSTEMIC_STREAK)]
    try:
        st, d = http(port, "/api/agents/bulk", body={
            "agent_ids": batch, "action": "wake-mode", "params": {"mode": "at"}})
        rows = results(d)
        tried = [r for r in rows if not r.get("skipped")]
        skipped = [r for r in rows if r.get("skipped")]
        check("identical repeated failures abort the batch as systemic",
              bool(d.get("aborted")) and len(tried) == web.BULK_SYSTEMIC_STREAK)
        check("the untried agents are reported as skipped, not as failures of "
              "their own, so a client can retry exactly those",
              len(skipped) == len(batch) - web.BULK_SYSTEMIC_STREAK
              and all(r["status"] == 503 for r in skipped))
        check("every agent still gets a row", len(rows) == len(batch))
    finally:
        web.NthWebHandler._apply_agent_action = real_apply

    # A batch of DIFFERENT failures must NOT be mistaken for a systemic one.
    seq = iter(range(100))

    def _explode_differently(self, agent_id, action, params, ident):
        raise RuntimeError(f"distinct failure {next(seq)}")

    web.NthWebHandler._apply_agent_action = _explode_differently
    try:
        st, d = http(port, "/api/agents/bulk", body={
            "agent_ids": batch, "action": "wake-mode", "params": {"mode": "at"}})
        check("distinct failures are not treated as systemic — every agent is "
              "still tried",
              not d.get("aborted")
              and not any(r.get("skipped") for r in results(d))
              and len(results(d)) == len(batch))
    finally:
        web.NthWebHandler._apply_agent_action = real_apply

    # ── placement: the one action with a BULK-SPECIFIC code path ──
    # `_apply_agent_action` accepts a `channels` LIST that only this endpoint
    # ever sends (the single-agent route sends one `channel`), so that branch
    # is this endpoint's to test.
    json.loads(srv.nth_connect(summary="t", name="Host2", channel="chan-c"))
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0]], "action": "placement",
        "params": {"channels": ["chan-c"], "present": True}})
    db = sqlite3.connect(str(srv.DB_PATH))
    placed = {r[0] for r in db.execute(
        "SELECT channel FROM agent_channels WHERE agent_id=?", (ids[0],))}
    db.close()
    check("placement accepts a channels LIST and applies it",
          st == 200 and "chan-c" in placed)
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0]], "action": "placement",
        "params": {"channels": ["chan-c", "no-such-channel"]}})
    check("an unknown channel in the list is rejected for that agent",
          results(d) and results(d)[0]["ok"] is False)
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0]], "action": "placement",
        "params": {"channels": [srv.AGENT_INBOX_CHANNEL]}})
    check("the private agent inbox cannot be changed through bulk either",
          results(d) and results(d)[0]["status"] == 400)
    st, d = http(port, "/api/agents/bulk", body={
        "agent_ids": [ids[0]], "action": "placement", "params": {"channels": []}})
    check("an empty channels list is rejected",
          results(d) and results(d)[0]["status"] == 400)

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
