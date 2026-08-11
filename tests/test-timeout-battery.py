"""Battery timeout ceiling test — configurable duration with unfakeable breadcrumbs.

Prints timestamped breadcrumbs with random tokens at regular intervals.
Designed to be run inside Haiku agents at various timeout values.

Usage: python test-timeout-battery.py --duration 3500 --interval 300

  --duration SECONDS   Total sleep time (default: 3500)
  --interval SECONDS   Breadcrumb interval (default: 300 = 5 min)
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


# Parse args
duration = 3500
interval = 300

args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--duration" and i + 1 < len(args):
        try:
            duration = int(args[i + 1])
        except ValueError:
            print(json.dumps({"error": f"Invalid duration: {args[i + 1]}"}))
            sys.exit(1)
    elif arg == "--interval" and i + 1 < len(args):
        try:
            interval = int(args[i + 1])
        except ValueError:
            print(json.dumps({"error": f"Invalid interval: {args[i + 1]}"}))
            sys.exit(1)

start = time.time()
start_token = random_token()

print(json.dumps({
    "phase": "start",
    "time": now(),
    "token": start_token,
    "pid": os.getpid(),
    "target_duration": duration,
    "interval": interval,
}), flush=True)

# Breadcrumbs at regular intervals
elapsed = 0
crumb_count = 0
while elapsed + interval < duration:
    time.sleep(interval)
    elapsed = round(time.time() - start)
    crumb_count += 1
    print(json.dumps({
        "phase": f"crumb_{crumb_count}",
        "time": now(),
        "token": random_token(),
        "elapsed": elapsed,
        "remaining": duration - elapsed,
    }), flush=True)

# Final sleep for remaining time
remaining = duration - (time.time() - start)
if remaining > 0:
    time.sleep(remaining)

end_time = now()
actual_elapsed = round(time.time() - start, 2)

print(json.dumps({
    "phase": "done",
    "time": end_time,
    "token": random_token(),
    "start_token": start_token,
    "word": "banana",
    "target_duration": duration,
    "actual_elapsed": actual_elapsed,
    "drift": round(actual_elapsed - duration, 2),
    "crumbs_printed": crumb_count,
}), flush=True)
