"""Test the restart architecture.

Simulates the messenger-foreground.py behavior with short durations.
Runs --cycles cap events before firing a real event.

Usage: python test-restart-arch.py --cycle-duration 10 --cycles 3

This will run 3 cycles of 10s each (printing restart each time),
then on the 4th run print a real event (new_messages).
Uses a counter file to track which cycle we're on.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

COUNTER_FILE = Path.home() / ".claude" / "roam" / "test-restart-counter.txt"


def now():
    return datetime.now(timezone.utc).isoformat()


def random_token():
    return os.urandom(8).hex()


cycle_duration = 10  # seconds per cycle
total_cycles = 3     # number of restart cycles before real event

args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--cycle-duration" and i + 1 < len(args):
        try:
            cycle_duration = int(args[i + 1])
        except ValueError:
            pass
    elif arg == "--cycles" and i + 1 < len(args):
        try:
            total_cycles = int(args[i + 1])
        except ValueError:
            pass

# Read and increment counter
current_cycle = 0
if COUNTER_FILE.exists():
    try:
        current_cycle = int(COUNTER_FILE.read_text().strip())
    except (ValueError, OSError):
        current_cycle = 0

token = random_token()

if current_cycle < total_cycles:
    # Not yet time for the real event — sleep then restart
    COUNTER_FILE.write_text(str(current_cycle + 1))

    print(json.dumps({
        "phase": "cycle_start",
        "cycle": current_cycle,
        "of": total_cycles,
        "time": now(),
        "token": token,
    }), flush=True)

    time.sleep(cycle_duration)

    print(json.dumps({
        "event": "restart",
        "cycle": current_cycle,
        "time": now(),
        "token": token,
        "msg": "RESTART ME — nothing happened",
    }))
else:
    # Real event — clean up counter and return
    COUNTER_FILE.unlink(missing_ok=True)

    print(json.dumps({
        "event": "new_messages",
        "cycle": current_cycle,
        "time": now(),
        "token": token,
        "word": "banana",
        "msg": f"Real event after {total_cycles} restart cycles",
    }))
