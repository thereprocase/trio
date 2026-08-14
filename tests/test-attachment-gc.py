"""Attachment GC: abandoned uploads, dead-channel rows, and orphan files.

Nothing collected these before, so every paste-then-abandon, every closed tab,
and every ended channel leaked a row and a file for the life of the install.
"""
import os
import sqlite3
import sys
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

_tmp = Path(tempfile.mkdtemp(prefix="nth-gc-"))
import nth_web as web                                    # noqa: E402
web.ATTACH_DIR = _tmp / "attachments"                    # keep the real dir safe
web.ATTACH_DIR.mkdir(parents=True, exist_ok=True)

failures = []
passed = 0


def check(name, cond):
    global passed
    if cond:
        passed += 1
        print("PASS: " + name)
    else:
        failures.append(name)
        print("FAIL: " + name)


def iso(sec_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=sec_ago)).isoformat()


DB = _tmp / "gc.db"
db = sqlite3.connect(str(DB))
db.row_factory = sqlite3.Row
db.execute("CREATE TABLE channels (code TEXT PRIMARY KEY, status TEXT)")
db.execute("INSERT INTO channels VALUES ('live', 'active')")
web.ensure_attachments_table(db)


def mkfile(name, age_s=0):
    d = web.ATTACH_DIR / "live"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x" * 8)
    if age_s:
        t = os.path.getmtime(f) - age_s
        os.utime(f, (t, t))
    return f


def add(chan, message_id, created_ago, name):
    f = mkfile(name, created_ago)
    db.execute(
        "INSERT INTO attachments (channel, message_id, member_id, mime, filename,"
        " path, created_at) VALUES (?,?,?,?,?,?,?)",
        (chan, message_id, "m1", "image/png", name, str(f), iso(created_ago)))
    db.commit()
    return f


# ── the three collectable kinds, and the three that must survive ────────────
keep_linked   = add("live", 7, 999999, "linked-old.png")     # published, ancient
keep_fresh    = add("live", None, 60, "fresh-unlinked.png")  # mid-compose
gone_abandon  = add("live", None, 999999, "abandoned.png")   # never sent
gone_deadchan = add("ghost", 9, 999999, "deadchan.png")      # channel cleaned up
orphan_old    = mkfile("orphan-old.png", 999999)             # no row, ancient
orphan_fresh  = mkfile("orphan-fresh.png", 0)                # no row, in flight
db.close()

stats = web.sweep_attachments(DB, force=True)

db = sqlite3.connect(str(DB))
db.row_factory = sqlite3.Row
rows = {r["filename"] for r in db.execute("SELECT filename FROM attachments")}
db.close()

check("published attachment is kept, however old", "linked-old.png" in rows)
check("an unlinked upload inside the grace period is kept",
      "fresh-unlinked.png" in rows)
check("an unlinked upload past the grace period is collected",
      "abandoned.png" not in rows)
check("an attachment whose channel is gone is collected",
      "deadchan.png" not in rows)

check("the abandoned upload's FILE is gone too", not gone_abandon.exists())
check("the dead channel's file is gone too", not gone_deadchan.exists())
check("a kept row's file is untouched", keep_linked.exists() and keep_fresh.exists())

check("an old orphan file with no row is collected", not orphan_old.exists())
check("a FRESH orphan file is left alone (upload may be in flight)",
      orphan_fresh.exists())

check("counts are reported", stats.get("abandoned") == 1
      and stats.get("dead_channel") == 1 and stats.get("orphan_files") == 1)

# ── a scratch DB must not delete another install's files ───────────────────
# attachments.path is absolute, so a row copied from a real database names a
# real file. Running against a scratch DB must not reach outside its own root.
outsider_dir = _tmp / "someone-elses-install" / "attachments" / "live"
outsider_dir.mkdir(parents=True, exist_ok=True)
outsider = outsider_dir / "not-ours.png"
outsider.write_bytes(b"precious")
db = sqlite3.connect(str(DB))
db.execute(
    "INSERT INTO attachments (channel, message_id, member_id, mime, filename,"
    " path, created_at) VALUES ('live', NULL, 'm1', 'image/png', 'not-ours.png', ?, ?)",
    (str(outsider), iso(999999)))
db.commit()
db.close()

web.sweep_attachments(DB, force=True)
check("a file outside the attachment root is NOT deleted", outsider.exists())

# ── the send/sweep race ────────────────────────────────────────────────────
# The doomed rows are SELECTed, then DELETEd as separate statements. A row can
# be LINKED by a concurrent /api/send in between — and the send holds
# BEGIN IMMEDIATE, so an unconditional delete does not lose the race, it queues
# behind the send and then destroys the attachment the user just posted
# successfully. The delete must be a compare-and-swap on the observed state.
race_dir = _tmp / "race"
(race_dir / "attachments" / "live").mkdir(parents=True, exist_ok=True)
saved_root, web.ATTACH_DIR = web.ATTACH_DIR, race_dir / "attachments"
try:
    rdb_path = race_dir / "race.db"
    c = sqlite3.connect(str(rdb_path))
    c.execute("CREATE TABLE channels (code TEXT PRIMARY KEY, status TEXT)")
    c.execute("INSERT INTO channels VALUES ('live','active')")
    web.ensure_attachments_table(c)
    rf = web.ATTACH_DIR / "live" / "raced.png"
    rf.write_bytes(b"posted")
    c.execute(
        "INSERT INTO attachments (channel, message_id, member_id, mime, filename,"
        " path, created_at) VALUES ('live', NULL, 'm1', 'image/png', 'raced.png',"
        " ?, ?)", (str(rf), iso(999999)))
    c.commit()

    # Stand in for the send committing between the sweep's SELECT and DELETE.
    # sqlite3.Connection is immutable, so wrap the connection the sweep opens.
    c.close()
    real_connect = sqlite3.connect
    fired = {"done": False}

    class RacingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if not fired["done"] and sql.lstrip().upper().startswith("BEGIN"):
                fired["done"] = True
                side = real_connect(str(rdb_path))
                side.execute("UPDATE attachments SET message_id = 7 "
                             "WHERE message_id IS NULL")
                side.commit()
                side.close()
            return self._inner.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __setattr__(self, name, value):
            if name == "_inner":
                object.__setattr__(self, name, value)
            else:
                setattr(self._inner, name, value)

    sqlite3.connect = lambda *a, **kw: RacingConn(real_connect(*a, **kw))
    try:
        web.sweep_attachments(rdb_path, force=True)
    finally:
        sqlite3.connect = real_connect

    c = real_connect(str(rdb_path))
    survived = c.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    c.close()
    check("the racing link actually fired", fired["done"] is True)
    check("a row linked between select and delete is NOT collected", survived == 1)
    check("and its file is left on disk", rf.exists())
finally:
    web.ATTACH_DIR = saved_root

# ── attach_dir_for: the function whose ABSENCE caused the incident ──────────
# Everything above monkeypatches web.ATTACH_DIR, which bypasses the derivation
# entirely — so without this the whole suite stays green even if the root goes
# back to being hardcoded, which is precisely the bug that lost real files.
# Under _tmp so the comparison is not defeated by /tmp -> /private/tmp on macOS.
db_a = _tmp / "install-a" / "nth.db"
db_b = _tmp / "install-b" / "nth.db"
db_a.parent.mkdir(parents=True, exist_ok=True)
db_b.parent.mkdir(parents=True, exist_ok=True)
root_a = web.attach_dir_for(db_a)
root_b = web.attach_dir_for(db_b)
check("attach_dir_for derives the root from the DB's own directory",
      root_a == (db_a.parent / "attachments").resolve())
check("two databases in different places get different roots", root_a != root_b)
check("the root is NOT the fixed home path",
      Path.home() / ".claude" / "nth" / "attachments" not in (root_a, root_b))

# ── the boundary itself, not just its rough direction ──────────────────────
def boundary_probe(age_s):
    """One unlinked row aged `age_s`; returns True if it survived a sweep."""
    d = Path(tempfile.mkdtemp(prefix="nth-bound-", dir=_tmp))
    (d / "attachments" / "live").mkdir(parents=True)
    saved_root, web.ATTACH_DIR = web.ATTACH_DIR, d / "attachments"
    try:
        bdb_path = d / "b.db"
        c = sqlite3.connect(str(bdb_path))
        c.execute("CREATE TABLE channels (code TEXT PRIMARY KEY, status TEXT)")
        c.execute("INSERT INTO channels VALUES ('live','active')")
        web.ensure_attachments_table(c)
        f = web.ATTACH_DIR / "live" / "b.png"
        f.write_bytes(b"x")
        c.execute(
            "INSERT INTO attachments (channel, message_id, member_id, mime,"
            " filename, path, created_at) VALUES ('live',NULL,'m1','image/png',"
            " 'b.png', ?, ?)", (str(f), iso(age_s)))
        c.commit(); c.close()
        web.sweep_attachments(bdb_path, force=True)
        c = sqlite3.connect(str(bdb_path))
        alive = c.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        c.close()
        return alive == 1
    finally:
        web.ATTACH_DIR = saved_root

check("just INSIDE the grace period is kept (pins the 24h value)",
      boundary_probe(web.ATTACH_GC_GRACE_S - 30) is True)
check("just OUTSIDE the grace period is collected (pins the 24h value)",
      boundary_probe(web.ATTACH_GC_GRACE_S + 30) is False)

# ── rate limiting: a burst of uploads must not sweep repeatedly ─────────────
again = web.sweep_attachments(DB)
check("a second sweep inside the interval is skipped", again.get("skipped") == 1)

# ── the MCP purge path must be contained too ────────────────────────────────
# nth_cleanup is a destructive tool any connected agent can call, and it deletes
# attachment files by the same absolute path column. It had no containment check
# at all until review caught it — the identical shape of bug that lost real
# files, in a second place.
import nth_server as srv                                  # noqa: E402

purge_root = _tmp / "purge" / "attachments"
(purge_root / "live").mkdir(parents=True, exist_ok=True)
srv.ATTACH_DIR = purge_root

pdb_path = _tmp / "purge" / "p.db"
pdb = sqlite3.connect(str(pdb_path))
pdb.row_factory = sqlite3.Row
web.ensure_attachments_table(pdb)

inside = purge_root / "live" / "ours.png"
inside.write_bytes(b"ours")
outside_dir = _tmp / "another-install" / "attachments" / "live"
outside_dir.mkdir(parents=True, exist_ok=True)
outside = outside_dir / "theirs.png"
outside.write_bytes(b"theirs")
for f in (inside, outside):
    pdb.execute(
        "INSERT INTO attachments (channel, message_id, member_id, mime, filename,"
        " path, created_at) VALUES ('live', 1, 'm1', 'image/png', ?, ?, ?)",
        (f.name, str(f), iso(10)))
pdb.commit()

doomed = srv._purge_channel_attachments(pdb, "live")

# The unlink must NOT have happened yet: this runs inside nth_cleanup's
# transaction, which is not committed until every channel is done. Unlinking
# inline made deletion permanent while the rows could still roll back on a
# later failure — leaving a LIVE channel full of rows pointing at files that
# no longer exist, the exact state the row-before-file order exists to avoid.
check("purge does not unlink before the transaction commits", inside.exists())
check("purge returns only paths inside its own attachment root",
      doomed == [str(inside.resolve())])

pdb.commit()
srv._unlink_purged(doomed, "live")
remaining = pdb.execute("SELECT COUNT(*) AS n FROM attachments").fetchone()["n"]
pdb.close()

check("purge removes the channel's rows", remaining == 0)
check("the contained file is unlinked after commit", not inside.exists())
check("a file outside the root is never unlinked", outside.exists())

# A rollback after purge must leave every file intact.
rb_root = _tmp / "rollback" / "attachments" / "live"
rb_root.mkdir(parents=True, exist_ok=True)
srv.ATTACH_DIR = _tmp / "rollback" / "attachments"
rb_db_path = _tmp / "rollback" / "r.db"
rdb = sqlite3.connect(str(rb_db_path))
rdb.row_factory = sqlite3.Row
web.ensure_attachments_table(rdb)
survivor = rb_root / "survivor.png"
survivor.write_bytes(b"keep me")
rdb.execute(
    "INSERT INTO attachments (channel, message_id, member_id, mime, filename,"
    " path, created_at) VALUES ('live', 1, 'm1', 'image/png', 'survivor.png', ?, ?)",
    (str(survivor), iso(10)))
rdb.commit()
_ = srv._purge_channel_attachments(rdb, "live")
rdb.rollback()                      # a later channel failed; nothing commits
still = rdb.execute("SELECT COUNT(*) AS n FROM attachments").fetchone()["n"]
rdb.close()
check("a rolled-back purge keeps both the row and its file",
      still == 1 and survivor.exists())

shutil.rmtree(_tmp, ignore_errors=True)
print()
print(("FAILED" if failures else "OK") + f" — {passed} passed, {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
