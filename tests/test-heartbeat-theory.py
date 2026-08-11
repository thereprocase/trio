"""Test whether stdout output resets the bash timeout timer.

Prints a heartbeat every --interval seconds for --duration total.
If the timeout resets on output, this process survives even with
a timeout shorter than --duration.

Usage: python test-heartbeat-theory.py --duration 300 --interval 30
"""
import json
import os
import sys
import time
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def random_token():
    return os.urandom(8).hex()


duration = 300  # 5 minutes total
interval = 30   # heartbeat every 30s

args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--duration" and i + 1 < len(args):
        try:
            duration = int(args[i + 1])
        except ValueError:
            pass
    elif arg == "--interval" and i + 1 < len(args):
        try:
            interval = int(args[i + 1])
        except ValueError:
            pass

start = time.time()
start_token = random_token()

print(json.dumps({
    "phase": "start",
    "time": now(),
    "token": start_token,
    "pid": os.getpid(),
    "duration": duration,
    "interval": interval,
}), flush=True)

elapsed = 0
beat = 0
while elapsed < duration:
    time.sleep(interval)
    elapsed = round(time.time() - start)
    beat += 1
    print(json.dumps({
        "phase": f"beat_{beat}",
        "time": now(),
        "token": random_token(),
        "elapsed": elapsed,
    }), flush=True)

print(json.dumps({
    "phase": "done",
    "time": now(),
    "token": random_token(),
    "start_token": start_token,
    "word": "banana",
    "actual_elapsed": round(time.time() - start, 2),
}), flush=True)
