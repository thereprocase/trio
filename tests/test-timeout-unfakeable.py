"""Unfakeable timeout ceiling test.

Prints timestamped breadcrumbs at intervals that prove continued execution.
Includes random tokens that can't be predicted or fabricated.
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

# Phase 1: immediate proof of life
start_token = random_token()
start_time = now()
print(json.dumps({
    "phase": "start",
    "time": start_time,
    "token": start_token,
    "pid": os.getpid(),
}), flush=True)

# Phase 2: breadcrumbs every 2 minutes
for minute in [2, 4, 6, 8, 10, 12, 14]:
    time.sleep(120)
    crumb_token = random_token()
    print(json.dumps({
        "phase": f"crumb_{minute}min",
        "time": now(),
        "token": crumb_token,
        "elapsed_target": minute * 60,
    }), flush=True)

# Phase 3: 15-minute mark — the real test
time.sleep(60)
fifteen_token = random_token()
fifteen_time = now()
print(json.dumps({
    "phase": "fifteen_minutes",
    "time": fifteen_time,
    "token": fifteen_token,
    "word": "banana",
    "elapsed_target": 900,
}), flush=True)

# Phase 4: one more minute to prove we're still here
time.sleep(60)
end_token = random_token()
end_time = now()
elapsed = time.time() - time.mktime(datetime.fromisoformat(start_time).timetuple())

print(json.dumps({
    "phase": "done",
    "time": end_time,
    "token": end_token,
    "start_token": start_token,
    "fifteen_token": fifteen_token,
    "word": "banana",
    "elapsed_approx": round(elapsed),
    "summary": f"survived {round(elapsed)}s — started {start_time}, ended {end_time}",
}), flush=True)
