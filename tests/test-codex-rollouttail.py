#!/usr/bin/env python3
"""Regression tests for codex_context_publisher's rollout parser.

The rollout JSONL is an external, unversioned format this repo does not
control, and every failure mode here is SILENT — a bad line is skipped, a
missing field stays empty, snapshot() returns None. Nothing crashes; the
context ring just goes dark or wrong. That is precisely what a live fleet
will not surface on its own.

Stdlib only. Run directly:  python3 tests/test-codex-rollouttail.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "server"))

import codex_context_publisher as pub  # noqa: E402

FAIL = 0


def check(label, cond):
    global FAIL
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAIL += 1


def write(path, *objs):
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")


META = {"type": "session_meta",
        "payload": {"session_id": "sess-1", "cwd": "/tmp/work"}}
# turn_context is a TOP-LEVEL type whose payload has no "type" key — the
# fallback in scan() exists for exactly this and broke once already.
TURN = {"type": "turn_context",
        "payload": {"model": "gpt-5.6-sol", "effort": "high"}}


def tc(total, window=400000, ts="2026-08-11T18:50:05Z"):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count",
                        "info": {"last_token_usage": {"total_tokens": total},
                                 "model_context_window": window}}}


tmp = tempfile.mkdtemp(prefix="rollouttail-")

print("dual schema (nested event_msg + top-level turn_context)")
p = os.path.join(tmp, "a.jsonl")
write(p, META, TURN, tc(120000))
t = pub.RolloutTail(p)
t.scan()
check("session_id from session_meta", t.session_id == "sess-1")
check("model from top-level turn_context", t.model == "gpt-5.6-sol")
check("effort from top-level turn_context", t.effort == "high")
check("context_window from nested token_count", t.context_window == 400000)
snap = t.snapshot("Codex-Sol", "codex-sol")
check("used_pct = 120000/400000 = 30.0", snap["used_pct"] == 30.0)

print("missing model key does not raise")
p = os.path.join(tmp, "b.jsonl")
write(p, META, {"type": "turn_context", "payload": {"effort": "low"}}, tc(1000))
t = pub.RolloutTail(p)
t.scan()
check("model stays empty", t.model == "")
check("effort still parsed", t.effort == "low")

print("no context window -> snapshot() is None, not ZeroDivisionError")
p = os.path.join(tmp, "c.jsonl")
write(p, META, {"type": "event_msg", "payload": {"type": "token_count",
      "info": {"last_token_usage": {"total_tokens": 5}}}})
t = pub.RolloutTail(p)
t.scan()
check("snapshot() returns None", t.snapshot("n", "s") is None)

print("used > window clamps at 100, never above")
p = os.path.join(tmp, "d.jsonl")
write(p, META, TURN, tc(999999, window=400000))
t = pub.RolloutTail(p)
t.scan()
check("clamped to 100.0", t.snapshot("n", "s")["used_pct"] == 100.0)

print("truncated final line is buffered, then completed")
p = os.path.join(tmp, "e.jsonl")
write(p, META, TURN)
with open(p, "a", encoding="utf-8") as f:
    f.write(json.dumps(tc(80000))[:40])          # half a line, no newline
t = pub.RolloutTail(p)
t.scan()
check("partial line not parsed yet", t.last_usage is None)
with open(p, "a", encoding="utf-8") as f:
    f.write(json.dumps(tc(80000))[40:] + "\n")   # rest of it
t.scan()
check("reassembled line parses", t.last_usage
      and t.last_usage.get("total_tokens") == 80000)

print("truncation/rotation resets offset without raising")
p = os.path.join(tmp, "f.jsonl")
write(p, META, TURN, tc(120000))
t = pub.RolloutTail(p)
t.scan()
before = t.offset
write(p, META)                                   # file now much shorter
t.scan()
check("offset reset after truncation", t.offset < before)

print("garbage line between valid lines is skipped")
p = os.path.join(tmp, "g.jsonl")
with open(p, "w", encoding="utf-8") as f:
    f.write(json.dumps(META) + "\n")
    f.write("{not json at all\n")
    f.write(json.dumps(tc(60000)) + "\n")
t = pub.RolloutTail(p)
t.scan()
check("valid lines still parsed around garbage",
      t.last_usage and t.last_usage.get("total_tokens") == 60000)

print("newest_rollout: exact session id beats a newer decoy")
home = os.path.join(tmp, "codexhome", "sessions", "2026", "08", "11")
os.makedirs(home)
want = os.path.join(home, "rollout-2026-08-11T15-12-47-019ff23d-d830-aaaa.jsonl")
decoy = os.path.join(home, "rollout-2026-08-11T15-12-47-019ff23d-d878-bbbb.jsonl")
write(want, META)
write(decoy, META)
os.utime(decoy, (9e9, 9e9))                      # decoy is far newer
got = pub.newest_rollout(os.path.join(tmp, "codexhome"), "019ff23d-d830-aaaa")
check("pinned to the exact id, not the newest file", got == want)
check("no session id -> newest wins",
      pub.newest_rollout(os.path.join(tmp, "codexhome"), "") == decoy)
check("unknown id -> None",
      pub.newest_rollout(os.path.join(tmp, "codexhome"), "nope-nope") is None)
check("missing codex home -> None",
      pub.newest_rollout(os.path.join(tmp, "does-not-exist"), "") is None)

print("codex_has_open: missing file is not 'open'")
check("nonexistent path -> False",
      pub.codex_has_open(os.path.join(tmp, "nope.jsonl"), _cache_s=0) is False)

print("")
print("FAILED" if FAIL else "all checks passed")
sys.exit(1 if FAIL else 0)
