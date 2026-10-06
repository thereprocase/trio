#!/usr/bin/env python3
"""Codex App Server notifications survive a client stop/start cycle."""
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))

from nth_codex_runtime import CodexAppServerClient


failures = 0


def check(label, condition):
    global failures
    print(("PASS" if condition else "FAIL") + ": " + label)
    if not condition:
        failures += 1


seen = []
client = CodexAppServerClient(
    command=[sys.executable, str(HERE / "fake_codex_app_server.py")],
    on_notification=lambda message: seen.append(message.get("method")),
)


def run_turn():
    client.start()
    thread_id = client.request("thread/start", {})["thread"]["id"]
    client.request("turn/start", {"threadId": thread_id, "input": []})
    deadline = time.time() + 2
    while "turn/completed" not in seen and time.time() < deadline:
        time.sleep(0.01)


try:
    run_turn()
    check("notifications are delivered before a restart",
          "turn/started" in seen and "turn/completed" in seen)

    client.stop()
    seen.clear()

    run_turn()
    check("notifications are still delivered after stop(); start()",
          "turn/started" in seen and "turn/completed" in seen)
finally:
    client.stop()

print(f"\n{'OK' if failures == 0 else 'FAILED'} — {failures} failure(s)")
raise SystemExit(1 if failures else 0)
