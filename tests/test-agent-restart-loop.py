"""Test agent restart loop durability.

Exits with a "cap" event after --duration seconds, simulating the sentinel's
max_runtime behavior. The Haiku agent should relaunch this script on cap events.

This tests the AGENT's ability to loop, not the bash timeout ceiling.

Usage: python test-agent-restart-loop.py --duration 120 --run-id 1
"""
import json
import os
import sys
import time
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


duration = 120  # 2 minutes per run
run_id = 0

args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--duration" and i + 1 < len(args):
        try:
            duration = int(args[i + 1])
        except ValueError:
            pass
    elif arg == "--run-id" and i + 1 < len(args):
        try:
            run_id = int(args[i + 1])
        except ValueError:
            pass

start = time.time()
token = os.urandom(8).hex()

print(json.dumps({
    "phase": "start",
    "run_id": run_id,
    "time": now(),
    "token": token,
    "pid": os.getpid(),
    "target_duration": duration,
}), flush=True)

time.sleep(duration)

elapsed = round(time.time() - start, 2)

print(json.dumps({
    "event": "cap",
    "run_id": run_id,
    "time": now(),
    "token": token,
    "word": "banana",
    "actual_elapsed": elapsed,
    "msg": "Cap reached. Relaunch with incremented --run-id.",
}), flush=True)
