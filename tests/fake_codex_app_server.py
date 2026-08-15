#!/usr/bin/env python3
"""Tiny deterministic Codex App Server used by Phase 5 tests."""
import json
import os
import sys


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


threads = {}
next_thread = 1
hold_turns = "--hold" in sys.argv
held = []
last_approval = ""
last_input = []
last_injected_items = []
last_thread_start = {}


TOOL_NAMES = (
    "connect", "send", "dm", "poll", "ack", "pounds", "ask",
    "claim", "complete", "cancel", "release", "lock", "unlock",
    "set_status", "rename", "status", "roster", "history", "end",
    "list", "cull", "cleanup", "retract",
)


def complete_turn(tid, turn_id, request_id, text):
    send({"method": "item/completed", "params": {
        "threadId": tid, "turnId": turn_id,
        "item": {"id": "item_" + str(request_id), "type": "agentMessage",
                 "text": "Codex echo: " + text}}})
    threads[tid]["active"] = None
    send({"method": "turn/completed", "params": {
        "threadId": tid, "turn": {"id": turn_id, "status": "completed"}}})

for raw in sys.stdin:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    params = msg.get("params") or {}
    request_id = msg.get("id")
    if request_id == 5000 and method is None:
        last_approval = (msg.get("result") or {}).get("decision") or ""
        continue
    if method == "initialized":
        continue
    if request_id is None:
        continue
    if method == "initialize":
        send({"id": request_id, "result": {
            "userAgent": "fake-codex/0.1", "platformFamily": "unix",
            "platformOs": "test"}})
    elif method == "account/read":
        send({"id": request_id, "result": {
            "account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}})
    elif method == "account/rateLimits/read":
        send({"id": request_id, "result": {
            "rateLimits": {
                "limitId": "codex", "limitName": "Codex",
                "primary": {"usedPercent": 25, "windowDurationMins": 300,
                            "resetsAt": 1786300000},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080,
                              "resetsAt": 1786900000},
                "rateLimitReachedType": None},
            "rateLimitsByLimitId": None,
            "rateLimitResetCredits": None}})
    elif method == "account/usage/read":
        send({"id": request_id, "result": {
            "summary": {"lifetimeTokens": 1234567, "peakDailyTokens": 45678,
                        "longestRunningTurnSec": 540, "currentStreakDays": 8,
                        "longestStreakDays": 14},
            "dailyUsageBuckets": [
                {"startDate": "2026-08-08", "tokens": 12000},
                {"startDate": "2026-08-09", "tokens": 34000}]}})
    elif method == "model/list":
        # Two models on purpose. One is not enough to exercise a model SWITCH
        # (any other id 400s as unknown first), and `minimal` is a Codex effort
        # with no Claude equivalent — the case where checking a Claude-shaped
        # allowlist first made an advertised effort permanently unreachable.
        send({"id": request_id, "result": {"data": [{
            "id": "fake-codex", "model": "fake-codex",
            "displayName": "Fake Codex", "hidden": False,
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "fast"},
                {"reasoningEffort": "high", "description": "deep"}],
            "defaultReasoningEffort": "low", "inputModalities": ["text"]
        }, {
            "id": "fake-codex-mini", "model": "fake-codex-mini",
            "displayName": "Fake Codex Mini", "hidden": False,
            "supportedReasoningEfforts": [
                {"reasoningEffort": "minimal", "description": "cheapest"},
                {"reasoningEffort": "low", "description": "fast"}],
            "defaultReasoningEffort": "minimal", "inputModalities": ["text"]
        }], "nextCursor": None}})
    elif method == "mcpServerStatus/list":
        send({"id": request_id, "result": {"data": [{
            "name": "nth-trio", "tools": {
                "trio_" + name: {"name": "trio_" + name}
                for name in TOOL_NAMES},
            "authStatus": "unsupported"}], "nextCursor": None}})
    elif method == "thread/start":
        last_thread_start = dict(params)
        tid = f"thr_fake_{os.getpid()}_{next_thread}"
        next_thread += 1
        threads[tid] = {"active": None}
        send({"id": request_id, "result": {"thread": {
            "id": tid, "sessionId": tid, "ephemeral": False}}})
        send({"method": "thread/started", "params": {"thread": {"id": tid}}})
    elif method == "thread/resume":
        tid = params.get("threadId")
        threads.setdefault(tid, {"active": None})
        send({"id": request_id, "result": {"thread": {"id": tid}}})
        send({"method": "thread/started", "params": {"thread": {"id": tid}}})
    elif method == "turn/start":
        tid = params.get("threadId")
        turn_id = "turn_" + str(request_id)
        threads.setdefault(tid, {})["active"] = turn_id
        text = ""
        last_input = params.get("input") or []
        for item in last_input:
            if item.get("type") == "text":
                text += item.get("text") or ""
        send({"id": request_id, "result": {"turn": {
            "id": turn_id, "status": "inProgress", "items": []}}})
        send({"method": "turn/started", "params": {
            "threadId": tid, "turn": {"id": turn_id, "status": "inProgress"}}})
        if hold_turns:
            held.append((tid, turn_id, request_id, text))
        else:
            complete_turn(tid, turn_id, request_id, text)
    elif method == "fake/complete":
        if held:
            complete_turn(*held.pop(0))
        send({"id": request_id, "result": {}})
    elif method == "fake/request-approval":
        tid = params.get("threadId")
        send({"id": 5000, "method": "item/commandExecution/requestApproval",
              "params": {"threadId": tid, "turnId": "turn_approval",
                         "itemId": "item_approval", "reason": "test command",
                         "command": "git status", "cwd": "/tmp"}})
        send({"id": request_id, "result": {"approvalId": 5000}})
    elif method == "fake/approval-result":
        send({"id": request_id, "result": {"decision": last_approval}})
    elif method == "fake/last-input":
        send({"id": request_id, "result": {"input": last_input}})
    elif method == "fake/last-thread-start":
        send({"id": request_id, "result": {"params": last_thread_start}})
    elif method == "fake/last-injected-items":
        send({"id": request_id, "result": {"items": last_injected_items}})
    elif method == "fake/crash":
        raise SystemExit(7)
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
    elif method == "thread/inject_items":
        last_injected_items = params.get("items") or []
        send({"id": request_id, "result": {}})
    elif method in ("thread/unsubscribe", "thread/archive", "thread/delete",
                    "thread/compact/start"):
        send({"id": request_id, "result": {}})
    elif method == "explode":
        send({"id": request_id, "error": {"code": 99, "message": "boom"}})
    elif method == "emit/request":
        send({"id": 9999, "method": "item/commandExecution/requestApproval",
              "params": {"reason": "test"}})
        send({"id": request_id, "result": {}})
    else:
        send({"id": request_id, "error": {
            "code": -32601, "message": "unknown method: " + str(method)}})
