"""The wake filter is a spec/status pair, not one column doing both jobs.

members.filter_mode is STATUS: the monitor publishes its effective mode there
on every heartbeat, and peers read it off the roster to decide whether an
ambient post will actually be heard before spending tokens writing it.

members.filter_mode_requested is SPEC: the operator's override, written by the
dashboard and by nothing else. NULL means "no override, use the launch arg".

Effective mode = requested ?? launch arg. One column could not do both — the
writer of the status overwrites the request on its next heartbeat, which is
exactly how a dashboard change used to revert within 10 seconds.

Drives the REAL monitor() loop against a temporary sqlite DB.

Usage: python tests/test-monitor-filter-mode.py
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import nth_monitor as nm   # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_db(path, filter_mode="__null__", with_column=True):
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    member_cols = (
        "id TEXT, channel TEXT, name TEXT, last_seen TEXT,"
        " last_read INTEGER DEFAULT 0, status_text TEXT DEFAULT '',"
        " messenger_heartbeat TEXT DEFAULT '', watchdog_heartbeat TEXT DEFAULT ''"
    )
    if with_column:
        # Mirror PRODUCTION's declaration. An earlier version of this fixture
        # declared filter_mode nullable while nth_server declares it
        # NOT NULL DEFAULT 'all', so case 1 tested a state a real database can
        # never be in — and the suite stayed green while the feature was
        # inverted in production.
        member_cols += (", filter_mode TEXT NOT NULL DEFAULT 'all'"
                        ", filter_mode_requested TEXT")
    db.executescript(
        f"""
        CREATE TABLE channels (code TEXT PRIMARY KEY, status TEXT, ended_by TEXT);
        CREATE TABLE members ({member_cols}, PRIMARY KEY (id, channel));
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, member_id TEXT,
            member_name TEXT, content TEXT, created_at TEXT,
            mentions TEXT DEFAULT '', refs TEXT DEFAULT '', bangs TEXT DEFAULT ''
        );
        CREATE TABLE sessions (
            channel TEXT, member_id TEXT, last_read INTEGER DEFAULT 0, revoked_at TEXT
        );
        CREATE TABLE tasks (channel TEXT, claimed_by TEXT, status TEXT);
        """
    )
    db.execute("INSERT INTO channels (code, status) VALUES ('CHAN', 'active')")
    if with_column and filter_mode != "__null__":
        db.execute("INSERT INTO members (id, channel, name, filter_mode_requested) "
                   "VALUES ('m1', 'CHAN', 'Alice', ?)", (filter_mode,))
    else:
        db.execute("INSERT INTO members (id, channel, name) "
                   "VALUES ('m1', 'CHAN', 'Alice')")
    db.commit()
    db.close()


class Capture:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event_dict):
        # nm.emit is module-global, so a monitor thread from an EARLIER case is
        # still running and still calling it. Without this guard those events
        # land in whichever Capture is current and the case-5 assertion picks
        # up case-4's errors — a cross-case leak that looks like a real bug.
        if nm.emit is not self:
            return
        with self.lock:
            self.events.append(event_dict)

    def snapshot(self):
        with self.lock:
            return list(self.events)


def run_monitor(db_path, launch_filter="all"):
    cap = Capture()
    nm.emit = cap
    t = threading.Thread(
        target=nm.monitor,
        kwargs={"channel": "CHAN", "member_id": "m1",
                "filter_mode": launch_filter, "_db_path": str(db_path)},
        daemon=True,
    )
    t.start()
    return cap, t


def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def col(db_path, name="filter_mode"):
    c = sqlite3.connect(str(db_path))
    try:
        row = c.execute(
            f"SELECT {name} FROM members WHERE id='m1' AND channel='CHAN'"
        ).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def set_mode(db_path, mode):
    """Write the REQUEST, the way the dashboard endpoint does."""
    c = sqlite3.connect(str(db_path))
    try:
        c.execute("UPDATE members SET filter_mode_requested=? "
                  "WHERE id='m1' AND channel='CHAN'", (mode,))
        c.commit()
    finally:
        c.close()


def post(db_path, content, mentions=None, bangs=None):
    """mentions/bangs are lists of member ids. The server stores the sigil
    arrays as JSON, and _parse_id_list() json.loads them — a bare 'm1' parses
    to [] and would silently never match."""
    c = sqlite3.connect(str(db_path))
    try:
        c.execute(
            "INSERT INTO messages (channel, member_id, member_name, content,"
            " created_at, mentions, bangs) VALUES ('CHAN','other','Bob',?,?,?,?)",
            (content, now_iso(), json.dumps(mentions or []),
             json.dumps(bangs or [])))
        c.commit()
    finally:
        c.close()


def woke_for(cap, needle):
    return any(e.get("event") == "new_messages"
               and needle in str(e) for e in cap.snapshot())


def heartbeat_written(db_path):
    return bool(col(db_path, "messenger_heartbeat"))


tmpdir = Path(tempfile.mkdtemp(prefix="nth-filter-test-"))
orig_emit = nm.emit

# ---------------------------------------------------------------------------
# Case 1: with no request, the monitor PUBLISHES the launch arg as status.
# Nothing is "seeded" — the monitor never writes the request column at all.
# ---------------------------------------------------------------------------
db1 = tmpdir / "seed.db"
build_db(db1, filter_mode="__null__")
check("case1: no request is recorded to begin with",
      col(db1, "filter_mode_requested") is None)
cap1, t1 = run_monitor(db1, launch_filter="at")
check("case1: the launch arg is published as status",
      wait_until(lambda: col(db1) == "at"))
check("case1: the monitor does NOT write the request column",
      col(db1, "filter_mode_requested") is None)

# ---------------------------------------------------------------------------
# Case 2: a value already in the DB WINS over the launch arg.
#         Launch with 'all' (wake on everything) but the DB says 'at'
#         (wake only on @me) — a plain broadcast must NOT wake us.
# ---------------------------------------------------------------------------
db2 = tmpdir / "dbwins.db"
build_db(db2, filter_mode="at")
cap2, t2 = run_monitor(db2, launch_filter="all")
check("case2: monitor started", wait_until(lambda: heartbeat_written(db2)))
check("case2: launch arg did NOT overwrite the stored mode", col(db2) == "at")

post(db2, "just a broadcast, nobody mentioned")
time.sleep(1.5)
check("case2: DB mode 'at' suppressed a plain broadcast "
      "(launch arg 'all' did not win)",
      not woke_for(cap2, "just a broadcast"))

post(db2, "hey @m1 look at this", mentions=["m1"])
check("case2: an @-mention still wakes under 'at'",
      wait_until(lambda: woke_for(cap2, "look at this"), timeout=4.0))

# ---------------------------------------------------------------------------
# Case 3: the heartbeat must NOT clobber an operator's change.
#         This is the actual regression — the old code wrote the launch arg
#         into the column on every 10s heartbeat.
# ---------------------------------------------------------------------------
db3 = tmpdir / "noclobber.db"
build_db(db3, filter_mode="all")
cap3, t3 = run_monitor(db3, launch_filter="all")
check("case3: monitor started", wait_until(lambda: heartbeat_written(db3)))

set_mode(db3, "at")           # the operator retunes from the dashboard
# HEARTBEAT_INTERVAL is 10s. Wait past one, because the whole point is that
# the heartbeat can no longer undo the operator: it writes the STATUS column,
# never the request.
deadline = time.monotonic() + nm.HEARTBEAT_INTERVAL + 3
stayed = True
while time.monotonic() < deadline:
    if col(db3, "filter_mode_requested") != "at":
        stayed = False
        break
    time.sleep(0.25)
check(f"case3: the request survived a {nm.HEARTBEAT_INTERVAL}s heartbeat",
      stayed)
check("case3: and the published status converged on it",
      wait_until(lambda: col(db3) == "at", timeout=nm.HEARTBEAT_INTERVAL + 5))

post(db3, "broadcast after the retune")
time.sleep(1.5)
check("case3: the new mode is in effect with no restart",
      not woke_for(cap3, "broadcast after the retune"))

# ---------------------------------------------------------------------------
# Case 4: an invalid stored mode FAILS OPEN — a bad write can never mute an
#         agent into silence.
# ---------------------------------------------------------------------------
db4 = tmpdir / "failopen.db"
build_db(db4, filter_mode="nonsense-not-a-mode")
cap4, t4 = run_monitor(db4, launch_filter="at")
check("case4: monitor started", wait_until(lambda: heartbeat_written(db4)))
post(db4, "plain broadcast under a bogus mode")
# NOT fail-open. Failing open means failing into the most EXPENSIVE mode, and
# every spurious wake is a billed turn — a typo in the dashboard should not
# cost money. The catastrophic "agent hears nothing" case is already covered
# by bangs, which no mode can suppress (asserted below).
check("case4: an unrecognised request keeps the last-known-good mode",
      not wait_until(lambda: woke_for(cap4, "plain broadcast under a bogus mode"),
                     timeout=2.5))
check("case4: and says so, rather than changing mode silently",
      any(e.get("event") == "error" and "unrecognised" in str(e.get("msg", ""))
          for e in cap4.snapshot()))
post(db4, "emergency", bangs=["m1"])
check("case4: a bang still wakes under an unrecognised request",
      wait_until(lambda: woke_for(cap4, "emergency"), timeout=4.0))

# ---------------------------------------------------------------------------
# Case 5: a pre-filter_mode schema falls back to the launch arg and does not
#         crash on the missing column.
# ---------------------------------------------------------------------------
db5 = tmpdir / "oldschema.db"
build_db(db5, with_column=False)
cap5, t5 = run_monitor(db5, launch_filter="at")
check("case5: monitor runs against a schema with no filter_mode column",
      wait_until(lambda: heartbeat_written(db5)))
post(db5, "broadcast on an old schema")
time.sleep(1.5)
check("case5: falls back to the launch arg ('at' suppressed the broadcast)",
      not woke_for(cap5, "broadcast on an old schema"))
check("case5: no error event was emitted",
      not any(e.get("event") == "error" for e in cap5.snapshot()))

nm.emit = orig_emit
print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK — 0 failure(s)")


# ---------------------------------------------------------------------------
# Case 6: the dashboard endpoint is what makes the request column a real
#         control surface. Without a writer, "the DB is the source of truth"
#         means "nobody can change it" — which is how the first version of
#         this feature removed the only working control (the launch arg) and
#         replaced it with one that did not exist.
# ---------------------------------------------------------------------------
import http.client                                            # noqa: E402
import urllib.request                                         # noqa: E402
import nth_server as srv                                      # noqa: E402
import nth_web as web                                         # noqa: E402

srvdir = tmpdir / "web"
srvdir.mkdir(exist_ok=True)
srv.DB_DIR = srvdir
srv.DB_PATH = srvdir / "nth.db"
r6 = json.loads(srv.nth_connect(summary="t", name="Alice", channel="filtchan"))
CH6, M6 = r6["channel"], r6["member_id"]

web.NthWebHandler.db_path = srv.DB_PATH
web.NthWebHandler.channel = ""
web.NthWebHandler.landing_mode = True
httpd = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
httpd.daemon_threads = True
p6 = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.2)


def api(path, body):
    data = json.dumps(body).encode()
    rq = urllib.request.Request(f"http://127.0.0.1:{p6}{path}", data=data, method="POST")
    rq.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(rq, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}


def requested_of(member_id):
    c = sqlite3.connect(str(srv.DB_PATH))
    try:
        row = c.execute("SELECT filter_mode_requested FROM members WHERE id=?",
                        (member_id,)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


try:
    st, _ = api(f"/api/member/filter?channel={CH6}",
                {"member_id": M6, "filter_mode": "about"})
    check("case6: the endpoint records the request", st == 200
          and requested_of(M6) == "about")

    st, _ = api(f"/api/member/filter?channel={CH6}",
                {"member_id": M6, "filter_mode": None})
    check("case6: null clears the override back to the launch arg",
          st == 200 and requested_of(M6) is None)

    st, _ = api(f"/api/member/filter?channel={CH6}",
                {"member_id": M6, "filter_mode": "loud"})
    check("case6: an unknown mode is rejected", st == 400)

    st, _ = api(f"/api/member/filter?channel={CH6}",
                {"member_id": "nosuch", "filter_mode": "at"})
    check("case6: an unknown member is a 404, not a silent no-op", st == 404)

    # The endpoint must never write the STATUS column: the member's own
    # monitor owns that, and would overwrite it on the next heartbeat anyway.
    api(f"/api/member/filter?channel={CH6}", {"member_id": M6, "filter_mode": "at"})
    c = sqlite3.connect(str(srv.DB_PATH))
    try:
        published = c.execute("SELECT filter_mode FROM members WHERE id=?",
                              (M6,)).fetchone()[0]
    finally:
        c.close()
    check("case6: the endpoint writes the spec, never the status",
          published == "all" and requested_of(M6) == "at")
finally:
    httpd.shutdown()
