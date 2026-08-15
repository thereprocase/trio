#!/usr/bin/env python3
"""Codex agent HTTP lifecycle through the provider-neutral control plane."""
import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
os.environ["TRIO_AGENT_CMD"] = f"{sys.executable} {HERE / 'fake_agent.py'}"
os.environ["TRIO_CODEX_CMD"] = f"{sys.executable} {HERE / 'fake_codex_app_server.py'}"

import nth_server as srv
import nth_web as web


failures = 0


def check(label, condition):
    global failures
    print(("PASS" if condition else "FAIL") + ": " + label)
    if not condition:
        failures += 1


def request(port, path, method="GET", body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw or b"{}")


tmp = Path(tempfile.mkdtemp(prefix="trio-web-codex-"))
srv.DB_DIR = tmp
srv.DB_PATH = tmp / "nth.db"
web._DB_PATH_GLOBAL = srv.DB_PATH
web.NthWebHandler.db_path = srv.DB_PATH
web.NthWebHandler._default_channel = ""
web.NthWebHandler._agent_control_enabled = True
web._SUPERVISOR = None
web._RUNTIME_HEALTH = {}
host = json.loads(srv.nth_connect(summary="test", name="Host", channel="codex-room"))

server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
server.daemon_threads = True
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
router = None
try:
    status, payload = request(port, "/api/agent-models?provider=codex")
    check("Codex models are discovered from App Server",
          status == 200 and payload["models"][0]["id"] == "fake-codex")

    status, payload = request(port, "/api/agents", "POST", {
        "provider": "codex", "model": "fake-codex", "effort": "high",
        "channels": ["codex-room"], "cwd": str(tmp),
        "permission_profile": "balanced", "wake_mode": "about",
        "name": "CodeFriend", "prompt": "Keep changes small.",
    })
    agent = payload.get("agent", {})
    agent_id = agent.get("id", "")
    check("Codex create starts a managed thread", status == 200 and agent.get("live"))
    check("Codex create returns provider controls",
          agent.get("provider") == "codex"
          and agent.get("permission_profile") == "balanced"
          and agent.get("wake_mode") == "about")

    status, payload = request(port, "/api/agents")
    found = next((a for a in payload.get("agents", []) if a["id"] == agent_id), {})
    check("roster exposes thread continuity without a per-agent PID",
          status == 200 and found.get("provider") == "codex"
          and str(found.get("runtime_ref", "")).startswith("thr_fake_")
          and found.get("pid") is None and found.get("queued") == 0)

    web.get_supervisor().codex._client.request(
        "fake/request-approval", {"threadId": found["runtime_ref"]})
    deadline = time.time() + 2
    approvals = []
    while not approvals and time.time() < deadline:
        status, approval_payload = request(port, "/api/approvals")
        approvals = approval_payload.get("approvals", [])
        if not approvals:
            time.sleep(0.02)
    check("approval inbox exposes pending Codex decisions",
          status == 200 and approvals and approvals[0]["command"] == "git status")
    approval_id = approvals[0]["id"]
    status, _ = request(
        port, f"/api/approvals/{approval_id}/resolve", "POST", {"decision": "decline"})
    check("approval endpoint resolves without blocking App Server", status == 200)
    status, activity = request(port, f"/api/agents/{agent_id}/activity")
    check("operator activity endpoint includes approval lifecycle",
          status == 200 and any(e["method"] == "approval/pending"
                                for e in activity.get("events", [])))

    status, _ = request(port, f"/api/agents/{agent_id}/wake-mode", "POST", {"mode": "all"})
    with sqlite3.connect(str(srv.DB_PATH)) as db:
        mode = db.execute("SELECT wake_mode FROM agents WHERE id=?", (agent_id,)).fetchone()[0]
    check("wake policy can be changed after creation", status == 200 and mode == "all")

    status, claude_payload = request(port, "/api/agents", "POST", {
        "provider": "claude", "model": "sonnet", "channels": ["codex-room"],
        "wake_mode": "at", "name": "ClaudeFriend"})
    claude_id = claude_payload.get("agent", {}).get("id", "")
    check("Claude and Codex can coexist in one managed room",
          status == 200 and claude_id and web.get_supervisor().is_running(claude_id))
    router = web.AgentRouter(srv.DB_PATH, web.get_supervisor(), interval=0.05)
    router.start()
    time.sleep(0.1)
    with sqlite3.connect(str(srv.DB_PATH)) as db:
        baseline = db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    srv.nth_send(channel="codex-room", member_id=host["member_id"],
                 message=f"@{agent_id} @{claude_id} compare notes")
    deadline = time.time() + 4
    responders = set()
    while time.time() < deadline:
        with sqlite3.connect(str(srv.DB_PATH)) as db:
            responders = {row[0] for row in db.execute(
                "SELECT member_id FROM messages WHERE id>? AND member_id IN (?,?)",
                (baseline, agent_id, claude_id)).fetchall()}
        if responders == {agent_id, claude_id}:
            break
        time.sleep(0.05)
    check("one directed room message wakes both providers",
          responders == {agent_id, claude_id})
    router.stop()
    router = None
    request(port, f"/api/agents/{claude_id}/archive", "POST")

    status, _ = request(port, f"/api/agents/{agent_id}/compact", "POST")
    check("Codex compact maps through the common action", status == 200)
    with sqlite3.connect(str(srv.DB_PATH)) as db:
        old_ref = db.execute("SELECT runtime_ref FROM agents WHERE id=?", (agent_id,)).fetchone()[0]
    status, _ = request(port, f"/api/agents/{agent_id}/clear", "POST")
    with sqlite3.connect(str(srv.DB_PATH)) as db:
        new_ref = db.execute("SELECT runtime_ref FROM agents WHERE id=?", (agent_id,)).fetchone()[0]
        archived = db.execute(
            "SELECT runtime_ref FROM agent_runtime_history WHERE agent_id=? AND disposition='cleared'",
            (agent_id,)).fetchone()
    check("Codex clear replaces and archives context",
          status == 200 and new_ref != old_ref and archived == (old_ref,))

    status, _ = request(port, f"/api/agents/{agent_id}/hibernate", "POST")
    check("Codex hibernate unloads its thread",
          status == 200 and not web.get_supervisor().is_running(agent_id))
    status, _ = request(port, f"/api/agents/{agent_id}/wake", "POST")
    check("Codex wake resumes the same durable thread",
          status == 200 and web.get_supervisor().is_running(agent_id))

    status, _ = request(port, "/api/agents", "POST", {
        "provider": "codex", "model": "missing", "cwd": str(tmp)})
    check("unknown Codex models fail before durable creation", status == 400)

    # ── LOTC review regressions ──────────────────────────────────────────
    sup = web.get_supervisor()

    # An approval must be visible even when the agent has NO channel
    # placement. POST /api/agents accepts "channels": [], and the old
    # placement filter hid exactly those agents — while they blocked for a
    # full 120s timeout with nobody able to answer.
    status, payload = request(port, "/api/agents", "POST", {
        "provider": "codex", "model": "fake-codex", "channels": [],
        "cwd": str(tmp), "name": "Unplaced",
    })
    unplaced = payload.get("agent", {}).get("id", "")
    check("an agent can be created with no channel placement",
          status == 200 and bool(unplaced))
    status, payload = request(port, "/api/agents")
    uref = next((a.get("runtime_ref") for a in payload.get("agents", [])
                 if a["id"] == unplaced), "")
    sup.codex._client.request("fake/request-approval", {"threadId": uref})
    deadline, seen = time.time() + 2, []
    while not seen and time.time() < deadline:
        status, ap = request(port, "/api/approvals")
        seen = [a for a in ap.get("approvals", []) if a.get("agent_id") == unplaced]
        if not seen:
            time.sleep(0.02)
    check("approvals are NOT filtered by channel placement", bool(seen))
    for a in seen:
        sup.resolve_approval(a["id"], "approved")

    # Hibernate is automatic — the idle reaper does it with no operator
    # action — and feed() already returned True for anything queued, so the
    # router counts it delivered. Dropping it silently loses real work.
    import collections as _collections
    ctx = {"channel": "codex-room", "text": "queued through hibernate",
           "attachments": [], "baseline": 0, "source_message_id": 0,
           "source_sender": ""}
    with sup.codex._lock:
        sup.codex._queued.setdefault(agent_id, _collections.deque()).append(ctx)
        sup.codex._active[agent_id] = "turn_pretend_busy"
    before = sup.queued_count(agent_id)
    sup.hibernate(agent_id)
    check("hibernate preserves queued prompts (they were silently dropped)",
          before == 1 and sup.queued_count(agent_id) == 1)
    with sup.codex._lock:
        sup.codex._queued.pop(agent_id, None)
        sup.codex._active.pop(agent_id, None)
    status, _ = request(port, f"/api/agents/{agent_id}/wake", "POST")
    check("wake after a queue-preserving hibernate still works", status == 200)

    # A live Codex thread takes its model from thread/start; neither
    # turn/start nor thread/resume carries one. Writing the row while it ran
    # left the runtime on the old model with the UI reporting success.
    status, _ = request(port, f"/api/agents/{agent_id}/model", "POST",
                        {"model": "fake-codex-mini"})
    check("changing a RUNNING Codex agent's model is refused, not silently lost",
          status == 409)

    # `minimal` is advertised by fake-codex-mini and is not in EFFORT_LEVELS.
    # Validating against the Claude-shaped list first made it unreachable no
    # matter what model/list returned.
    status, payload = request(port, "/api/agents", "POST", {
        "provider": "codex", "model": "fake-codex-mini", "effort": "minimal",
        "channels": [], "cwd": str(tmp), "name": "MinimalEffort",
    })
    _mid = payload.get("agent", {}).get("id", "")
    with sqlite3.connect(str(srv.DB_PATH)) as db:
        _eff = db.execute("SELECT effort FROM agents WHERE id=?", (_mid,)).fetchone()
    check("a Codex-only effort the provider advertises is accepted",
          status == 200 and _eff is not None and _eff[0] == "minimal")
    status, _ = request(port, "/api/agents", "POST", {
        "provider": "codex", "model": "fake-codex-mini", "effort": "nonsense",
        "channels": [], "cwd": str(tmp), "name": "BadEffort",
    })
    check("an effort no provider advertises is still rejected", status == 400)

    # interrupt must reach the provider's interrupt, not stop(). For Claude
    # the difference is ST_SLEEPING vs ST_STOPPED — a resumable session or a
    # discarded one.
    _seen = {}
    _real = sup.claude.interrupt
    sup.claude.interrupt = lambda aid: _seen.setdefault("called", aid) or True
    try:
        sup._provider_cache["ag_fake_claude"] = "claude"
        sup.interrupt("ag_fake_claude")
    finally:
        sup.claude.interrupt = _real
        sup._provider_cache.pop("ag_fake_claude", None)
    check("dispatcher routes Claude interrupt to interrupt(), not stop()",
          _seen.get("called") == "ag_fake_claude")

    status, _ = request(port, f"/api/agents/{agent_id}/archive", "POST")
    with sqlite3.connect(str(srv.DB_PATH)) as db:
        row = db.execute("SELECT archived_at FROM agents WHERE id=?", (agent_id,)).fetchone()
    check("Codex archive stops the thread and stamps archived_at (row retained)",
          status == 200 and row is not None and row[0] is not None
          and not web.get_supervisor().is_running(agent_id))
finally:
    if router is not None:
        router.stop()
    server.shutdown()
    if web._SUPERVISOR is not None:
        web._SUPERVISOR.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'OK' if failures == 0 else 'FAILED'} — {failures} failure(s)")
raise SystemExit(1 if failures else 0)
