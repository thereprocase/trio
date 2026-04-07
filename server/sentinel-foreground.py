"""Watchdog sentinel — run in FOREGROUND only.

Watches for cadence silence, flag inconsistency, and channel-ended events.
Loops internally on all other events. Exits cleanly 30s before
its runtime limit so the calling agent can restart it.

Only two possible outcomes:
  1. Real event detected → prints event JSON, exits
  2. Runtime limit approaching → prints restart JSON, exits

The calling agent should restart this script on outcome 2
and return to its parent on outcome 1.

Usage: python sentinel-foreground.py <channel> <member_id>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from roam_hive_mind_sentinel import (
    sentinel,
    DEFAULT_HEARTBEAT_THRESHOLD,
    DEFAULT_IDLE_HEARTBEAT_THRESHOLD,
    DEFAULT_SLEEP_CONFIRM,
)
from roam_constants import MAX_RUNTIME_S

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: sentinel-foreground.py <channel> <member_id>"
        }))
        sys.exit(1)

    result = sentinel(
        channel=sys.argv[1],
        member_id=sys.argv[2],
        max_runtime=MAX_RUNTIME_S,
        heartbeat_threshold=DEFAULT_HEARTBEAT_THRESHOLD,
        idle_heartbeat_threshold=DEFAULT_IDLE_HEARTBEAT_THRESHOLD,
        cadence_threshold=600,
        sleep_confirm=DEFAULT_SLEEP_CONFIRM,
        active_interval=30,
        idle_interval=30,
        watch_events=["cadence", "flag_inconsistency", "channel_ended"],
        role="watchdog",
    )

    if result.get("event") == "cap":
        print(json.dumps({
            "event": "restart",
            "msg": "RESTART ME — nothing happened",
        }))
    else:
        print(json.dumps(result))
