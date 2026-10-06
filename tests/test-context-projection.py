#!/usr/bin/env python3
"""Regression tests for nth_constants.project_context().

This is the allowlist that stops statusline/publisher snapshots from
carrying transcript paths, working directories, project dirs and API spend
onto an unauthenticated web page. It runs on three paths (hub monitor,
spoke monitor, server relay store), so a regression here is a data leak in
three places at once.

Stdlib only. Run directly:  python3 tests/test-context-projection.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "server"))

from nth_constants import (CONTEXT_MAX_STR, project_context)  # noqa: E402

FAIL = 0


def check(label, cond):
    global FAIL
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAIL += 1


# A realistic statusline snapshot: everything the UI reads, plus the
# sensitive fields it does not.
FULL = {
    "session_id": "abc-123",
    "session_name": "worker",
    "used_pct": 54.2,
    "cw_size": 1000000,
    "model": "claude-opus-5",
    "effort": "high",
    "harness": {
        "context_window": {"context_window_size": 1000000, "used": 542000},
        "rate_limits": {"five_hour": {"used_percentage": 12},
                        "seven_day": {"used_percentage": 40}},
        "transcript_path": "/home/someone/.claude/projects/x/session.jsonl",
        "cwd": "/home/someone/code/private-thing",
    },
    "workspace": {"project_dir": "/home/someone/code/private-thing"},
    "cost": {"total_cost_usd": 41.22},
    "cwd": "/home/someone/code/private-thing",
}

print("project_context — allowlist")
p = project_context(FULL)
check("keeps session_id", p.get("session_id") == "abc-123")
check("keeps used_pct", p.get("used_pct") == 54.2)
check("keeps model", p.get("model") == "claude-opus-5")
check("keeps effort", p.get("effort") == "high")
check("keeps harness.context_window",
      p["harness"]["context_window"]["context_window_size"] == 1000000)
check("keeps harness.rate_limits",
      p["harness"]["rate_limits"]["five_hour"]["used_percentage"] == 12)

print("project_context — drops sensitive fields")
check("drops top-level cwd", "cwd" not in p)
check("drops workspace", "workspace" not in p)
check("drops cost", "cost" not in p)
check("drops harness.transcript_path", "transcript_path" not in p["harness"])
check("drops harness.cwd", "cwd" not in p["harness"])

print("project_context — unknown keys cannot ride along")
p2 = project_context({"session_id": "s", "evil": "<script>x</script>",
                      "nested": {"deep": "secret"}})
check("unknown scalar dropped", "evil" not in p2)
check("unknown dict dropped", "nested" not in p2)

print("project_context — type safety")
check("None input -> None", project_context(None) is None)
check("str input -> None", project_context("nope") is None)
check("list input -> None", project_context([1, 2, 3]) is None)
check("int input -> None", project_context(42) is None)
check("empty dict -> empty dict", project_context({}) == {})

print("project_context — string caps")
long_model = "A" * (CONTEXT_MAX_STR * 3)
p3 = project_context({"session_id": "s", "model": long_model})
check("long string truncated to CONTEXT_MAX_STR",
      len(p3["model"]) == CONTEXT_MAX_STR)

print("project_context — harness subtree hygiene")
p4 = project_context({"session_id": "s",
                      "harness": {"context_window": "not-a-dict",
                                  "rate_limits": {"a": {"nested": "dict"}}}})
check("non-dict harness subtree dropped",
      "context_window" not in p4.get("harness", {}))
check("one nested level kept inside allowed subtree (rate_limits shape)",
      p4.get("harness", {}).get("rate_limits", {}) == {"a": {"nested": "dict"}})
p5 = project_context({"session_id": "s", "harness": "not-a-dict"})
check("non-dict harness dropped entirely", "harness" not in p5)

print("project_context — numeric passthrough")
p6 = project_context({"session_id": "s", "used_pct": 0, "cw_size": 0,
                      "data_age_s": None})
check("zero used_pct preserved (not treated as absent)", p6["used_pct"] == 0)
check("None passes through as None", p6["data_age_s"] is None)

print("")
print("FAILED" if FAIL else "all checks passed")
sys.exit(1 if FAIL else 0)
