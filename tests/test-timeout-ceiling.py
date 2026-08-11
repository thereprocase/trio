"""Timeout ceiling test — sleeps for --duration seconds, prints elapsed time.

Designed to be run inside a Haiku agent at various Bash timeout values
to empirically find the ceiling.

Usage: python test-timeout-ceiling.py --duration 300
"""
import json
import sys
import time

duration = 60  # default seconds

args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--duration" and i + 1 < len(args):
        try:
            duration = int(args[i + 1])
        except ValueError:
            print(json.dumps({"error": f"Invalid duration: {args[i + 1]}"}))
            sys.exit(1)

start = time.time()
print(json.dumps({"status": "started", "target_duration": duration}), flush=True)

time.sleep(duration)

elapsed = round(time.time() - start, 2)
print(json.dumps({
    "status": "completed",
    "target_duration": duration,
    "actual_elapsed": elapsed,
    "drift": round(elapsed - duration, 2),
}))
