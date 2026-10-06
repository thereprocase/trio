#!/usr/bin/env python3
"""A fake headless agent that speaks just enough of Claude Code's stream-json
protocol to exercise nth_supervisor WITHOUT a real, billed `claude` session.

Behaviour:
  * On start, emits a `system/init` line carrying a session_id. If launched
    with `--resume <id>`, it reports THAT id back (proving resume plumbing).
  * Reads stream-json `user` messages on stdin and echoes each back as an
    `assistant` message. EOF on stdin ends the process.

Pointed at via $TRIO_AGENT_CMD in tests.
"""
import json
import os
import sys


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    # Crash mode: die before emitting init → exercises the supervisor's
    # ST_ERRORED path (agent that never comes up).
    if os.environ.get("FAKE_AGENT_CRASH"):
        return 1

    argv = sys.argv[1:]
    resume = ""
    model = ""
    for i, a in enumerate(argv):
        if a == "--resume" and i + 1 < len(argv):
            resume = argv[i + 1]
        elif a == "--model" and i + 1 < len(argv):
            model = argv[i + 1]

    # Robustness probe: emit a non-dict JSON line before init. A correct reader
    # must skip it and still capture the session_id (Uruk-Hai bug).
    if os.environ.get("FAKE_AGENT_PREJUNK"):
        emit(123)
        emit([1, 2, 3])

    session_id = resume or f"sess-fake-{model or 'default'}-001"
    emit({"type": "system", "subtype": "init",
          "session_id": session_id, "model": model,
          "resumed": bool(resume)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = ""
        if isinstance(msg, dict):
            m = msg.get("message") or {}
            content = m.get("content", "") if isinstance(m, dict) else ""
        assistant_evt = {"type": "assistant",
                          "message": {"role": "assistant",
                                      "content": f"echo: {content}"},
                          "session_id": session_id}
        # Opt-in usage payload: exercises nth_supervisor's context-fullness
        # capture without every other fake_agent-driven test needing to know
        # about it. Lives on the ASSISTANT event's message.usage (matching
        # a real API response), not the result event — the result event's
        # usage is accumulated across a turn's internal API calls, not a
        # single request's actual context size. Format:
        # "input,cache_creation,cache_read" (all ints).
        usage_env = os.environ.get("FAKE_AGENT_USAGE_TOKENS")
        if usage_env:
            parts = [int(p) for p in usage_env.split(",")]
            while len(parts) < 3:
                parts.append(0)
            assistant_evt["message"]["usage"] = {
                "input_tokens": parts[0],
                "cache_creation_input_tokens": parts[1],
                "cache_read_input_tokens": parts[2],
            }
        emit(assistant_evt)
        emit({"type": "result", "subtype": "success", "is_error": False,
              "result": f"echo: {content}", "session_id": session_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
