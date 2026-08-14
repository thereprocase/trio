"""Regression checks for bounded session state and non-blocking dashboard hooks."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv  # noqa: E402

failures = []


def check(name, condition):
    print(("PASS" if condition else "FAIL") + f": {name}")
    if not condition:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="nth_session_"))
srv.DB_DIR = tmp
srv.DB_PATH = tmp / "nth.db"
db = srv.get_db()
now = datetime.now(timezone.utc)
old = (now - timedelta(days=8)).isoformat()
recent = (now - timedelta(hours=1)).isoformat()
db.executemany(
    "INSERT INTO sessions (session_token, member_id, channel, fingerprint, "
    "connected_at, last_seen, last_read, revoked_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
    [
        ("stale", "m1", "ch", "fp", old, old, None),
        ("expired", "m2", "ch", "fp", old, old, old),
        ("live", "m3", "ch", "fp", recent, recent, None),
    ],
)
srv._reap_sessions(db, now)
rows = {r["session_token"]: r for r in db.execute("SELECT * FROM sessions")}
check("reap revokes stale live sessions", rows["stale"]["revoked_at"] is not None)
check("reap removes old revoked sessions", "expired" not in rows)
check("reap retains recent sessions", rows["live"]["revoked_at"] is None)
indexes = {r["name"] for r in db.execute("PRAGMA index_list(sessions)")}
check("fingerprint index exists", "idx_sessions_fingerprint" in indexes)
db.commit()

# The hooks run on Claude's critical path. With a competing writer held, they
# must exit successfully in well under the old five-second busy timeout.
lock = sqlite3.connect(str(srv.DB_PATH), timeout=1, isolation_level=None)
lock.execute("BEGIN IMMEDIATE")
payloads = {
    "nth_activity_hook.py": {"hook_event_name": "PreToolUse", "session_id": "fp"},
    "nth_turn_hook.py": {"hook_event_name": "Stop", "session_id": "fp"},
}
try:
    for script, payload in payloads.items():
        t0 = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(SERVER / script)],
            input=json.dumps(payload), text=True, capture_output=True,
            env={**os.environ, "NTH_DB_PATH": str(srv.DB_PATH)}, timeout=1,
        )
        elapsed = time.monotonic() - t0
        check(f"{script}: busy DB exits successfully", result.returncode == 0)
        check(f"{script}: busy DB returns promptly", elapsed < 0.5)
finally:
    lock.execute("ROLLBACK")
    lock.close()
    db.close()

print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
