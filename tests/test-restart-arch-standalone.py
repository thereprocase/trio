"""Standalone restart arch test — no counter file needed.

Uses a --cycle argument to know which cycle it's on.
The calling agent increments the cycle number on restart.

Usage: python test-restart-arch-standalone.py --cycle-duration 15 --cycles 3 --cycle 0
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


cycle_duration = 15
total_cycles = 3
current_cycle = 0

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
    elif arg == "--cycle" and i + 1 < len(args):
        try:
            current_cycle = int(args[i + 1])
        except ValueError:
            pass

token = random_token()

if current_cycle < total_cycles:
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
        "next_cycle": current_cycle + 1,
        "time": now(),
        "token": token,
        "msg": "RESTART ME — nothing happened",
    }))
else:
    print(json.dumps({
        "event": "new_messages",
        "cycle": current_cycle,
        "time": now(),
        "token": token,
        "word": "banana",
        "msg": f"Real event after {total_cycles} restart cycles",
    }))
