"""Self-service buddy metadata: auth, uniqueness, and web propagation."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

tmp = Path(tempfile.mkdtemp(prefix="nth_avatar_"))
os.environ["NTH_HOME"] = str(tmp)

import nth_server as srv  # noqa: E402
import nth_web as web  # noqa: E402
from nth_constants import BUDDY_AVATARS  # noqa: E402

srv.DB_DIR = tmp
srv.DB_PATH = tmp / "nth.db"
failures = []


def check(name, condition):
    print(("PASS" if condition else "FAIL") + ": " + name)
    if not condition:
        failures.append(name)


def call(fn, **kwargs):
    return json.loads(fn(**kwargs))


try:
    alice = call(srv.nth_connect, summary="a", name="Alice", channel="avatars")
    bob = call(srv.nth_connect, summary="b", name="Bob", channel="avatars")
    check("connect advertises self-service buddy capability honestly",
          alice["buddy_icon"]["set_tool"].endswith("_set_avatar") and
          alice["buddy_icon"]["custom_generation"] is False)

    choices = call(srv.nth_avatar_choices, channel="avatars",
                   member_id=alice["member_id"],
                   session_token=alice["session_token"])
    names = [item["name"] for item in choices.get("choices", [])]
    check("choices come from the shared server allowlist",
          names == list(BUDDY_AVATARS))
    asset_names = sorted(path.parent.name for path in
                         (SERVER / "web" / "avatars").glob("*/avatar.svg"))
    check("every advertised buddy has exactly one checked-in SVG asset",
          sorted(BUDDY_AVATARS) == asset_names)

    chosen = BUDDY_AVATARS[5]
    conn = sqlite3.connect(str(srv.DB_PATH))
    active_before = conn.execute(
        "SELECT last_active_at FROM agents WHERE id=?",
        (alice["member_id"],)).fetchone()[0]
    conn.close()
    set_a = call(srv.nth_set_avatar, channel="avatars",
                 member_id=alice["member_id"], avatar_name=chosen,
                 session_token=alice["session_token"])
    check("agent sets its own checked-in buddy", set_a.get("avatar_name") == chosen)
    conn = sqlite3.connect(str(srv.DB_PATH))
    active_after = conn.execute(
        "SELECT last_active_at FROM agents WHERE id=?",
        (alice["member_id"],)).fetchone()[0]
    conn.close()
    check("buddy metadata does not falsify agent activity",
          active_after == active_before)

    stolen = call(srv.nth_set_avatar, channel="avatars",
                  member_id=bob["member_id"], avatar_name=BUDDY_AVATARS[6],
                  session_token=alice["session_token"])
    check("one agent cannot target another member id",
          "does not match" in stolen.get("error", ""))

    db = srv.get_db()
    ro = srv._mint_session_token(db, alice["member_id"], "avatars",
                                 role="read_only", fingerprint="avatar-ro")
    db.commit()
    db.close()
    denied = call(srv.nth_set_avatar, channel="avatars",
                  member_id=alice["member_id"], avatar_name=BUDDY_AVATARS[7],
                  session_token=ro)
    check("read-only capability cannot mutate buddy metadata",
          "read_only" in denied.get("error", ""))

    collision = call(srv.nth_set_avatar, channel="avatars",
                     member_id=bob["member_id"], avatar_name=chosen,
                     session_token=bob["session_token"])
    check("active buddy icons remain unique",
          "already in use" in collision.get("error", ""))
    unknown = call(srv.nth_set_avatar, channel="avatars",
                   member_id=bob["member_id"], avatar_name="../../evil.svg",
                   session_token=bob["session_token"])
    check("non-allowlisted paths are rejected", "Unknown" in unknown.get("error", ""))

    auto = call(srv.nth_set_avatar, channel="avatars",
                member_id=bob["member_id"], avatar_name="auto",
                session_token=bob["session_token"])
    check("auto reset chooses a distinct checked-in buddy",
          auto.get("avatar_name") in BUDDY_AVATARS and
          auto.get("avatar_name") != chosen)

    hub = web.EventHub(srv.DB_PATH, "avatars")
    q = hub.subscribe()
    events = []
    while not q.empty():
        events.append(json.loads(q.get_nowait()))
    roster = next(event for event in events if event.get("type") == "roster")
    by_id = {member["id"]: member for member in roster["members"]}
    check("web roster propagates the selected buddy URL",
          by_id[alice["member_id"]]["avatar_url"] ==
          f"/avatars/{chosen}/avatar.svg")
    check("web roster propagates auto selection too",
          by_id[bob["member_id"]]["avatar_url"] == auto["avatar_url"])
    hub.unsubscribe(q)

    conn = sqlite3.connect(str(srv.DB_PATH))
    row = conn.execute("SELECT avatar_name FROM agents WHERE id=?",
                       (alice["member_id"],)).fetchone()
    conn.close()
    check("selection is durable on the global agent identity", row[0] == chosen)

    # Both workers ask for the same currently-free portrait. BEGIN IMMEDIATE
    # must serialize the decision so exactly one can commit it.
    race_name = BUDDY_AVATARS[10]
    barrier = threading.Barrier(2)
    race_results = []
    def race_set(identity):
        barrier.wait()
        race_results.append(call(
            srv.nth_set_avatar, channel="avatars",
            member_id=identity["member_id"], avatar_name=race_name,
            session_token=identity["session_token"]))
    threads = [threading.Thread(target=race_set, args=(identity,))
               for identity in (alice, bob)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    check("concurrent setters cannot claim the same buddy",
          sum(result.get("avatar_name") == race_name for result in race_results) == 1
          and sum("already in use" in result.get("error", "")
                  for result in race_results) == 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
