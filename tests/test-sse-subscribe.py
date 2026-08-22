"""Regression coverage for the EventHub snapshot/live cutover."""
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv  # noqa: E402
import nth_web as web  # noqa: E402

failures = []


def check(name, condition):
    print(("PASS" if condition else "FAIL") + ": " + name)
    if not condition:
        failures.append(name)


tmp = tempfile.mkdtemp(prefix="nth_sse_cutover_")
try:
    srv.DB_DIR = Path(tmp)
    srv.DB_PATH = Path(tmp) / "nth.db"
    joined = json.loads(srv.nth_connect(summary="sender", name="Sender",
                                        channel="sse-cutover"))
    channel = joined["channel"]
    member = joined["member_id"]
    token = joined["session_token"]
    ids = []
    for i in range(web.HISTORY_LIMIT + 5):
        sent = json.loads(srv.nth_send(channel=channel, member_id=member,
                                      message=f"row-{i}", session_token=token))
        ids.append(sent["message_id"])

    hub = web.EventHub(srv.DB_PATH, channel)
    q = hub.subscribe()
    primed = []
    while not q.empty():
        primed.append(json.loads(q.get_nowait()))
    message_ids = [e["id"] for e in primed if e.get("type") == "message"]
    check("exactly HISTORY_LIMIT messages survive the control envelopes",
          len(message_ids) == web.HISTORY_LIMIT)
    check("prime retains the newest HISTORY_LIMIT ids in chronological order",
          message_ids == ids[-web.HISTORY_LIMIT:])
    hub.unsubscribe(q)

    entered = threading.Event()
    release = threading.Event()
    original_prime = hub._prime_payloads

    def paused_prime(*args, **kwargs):
        payloads = original_prime(*args, **kwargs)
        entered.set()
        release.wait(5)
        return payloads

    hub._prime_payloads = paused_prime
    result = {}
    subscriber = threading.Thread(target=lambda: result.setdefault("q", hub.subscribe()))
    subscriber.start()
    check("subscription reached the snapshot/live cutover", entered.wait(5))
    live_id = ids[-1] + 1
    live = {"type": "message", "id": live_id, "channel": channel,
            "member_id": member, "member_name": "Sender", "content": "racing-live",
            "mentions": [], "refs": [], "bangs": [], "recipients": [],
            "reply_to": None, "choices": None, "selection": None,
            "retracted_at": None, "retraction_reason": None, "edited_at": None,
            "created_at": "now", "attachments": []}
    broadcaster = threading.Thread(target=lambda: hub._broadcast(live))
    broadcaster.start()
    release.set()
    subscriber.join(5)
    broadcaster.join(5)
    race_q = result["q"]
    raced = []
    while not race_q.empty():
        raced.append(json.loads(race_q.get_nowait()))
    raced_ids = [e.get("id") for e in raced if e.get("type") == "message"]
    check("row arriving during subscribe is delivered exactly once",
          raced_ids.count(live_id) == 1)
    check("racing live row follows the complete chronological prime",
          raced_ids[-web.HISTORY_LIMIT - 1:] == ids[-web.HISTORY_LIMIT:] + [live_id])
    check("subscriber remains registered after a full 200-row prime plus live row",
          hub.subscriber_count() == 1)
    hub.unsubscribe(race_q)

    # Deterministically stop between fan-out and high-water advancement.  The
    # production loop holds the same lock across both operations, so a new
    # subscriber must wait, then include the row in its snapshot exactly once.
    boundary_row = json.loads(srv.nth_send(
        channel=channel, member_id=member, message="boundary-row",
        session_token=token))["message_id"]
    hub.last_msg_id = ids[-1]

    class LiveThread:
        @staticmethod
        def is_alive():
            return True

    hub._thread = LiveThread()
    boundary_entered = threading.Event()
    boundary_release = threading.Event()
    boundary_event = dict(live, id=boundary_row, content="boundary-row")

    def fanout_then_advance():
        with hub._lock:
            hub._broadcast_locked(boundary_event)
            boundary_entered.set()
            boundary_release.wait(5)
            hub.last_msg_id = boundary_row

    fanout = threading.Thread(target=fanout_then_advance)
    fanout.start()
    check("test paused after fan-out but before high-water advancement",
          boundary_entered.wait(5))
    boundary_result = {}
    boundary_sub = threading.Thread(
        target=lambda: boundary_result.setdefault("q", hub.subscribe()))
    boundary_sub.start()
    boundary_release.set()
    fanout.join(5)
    boundary_sub.join(5)
    boundary_q = boundary_result["q"]
    boundary_events = []
    while not boundary_q.empty():
        boundary_events.append(json.loads(boundary_q.get_nowait()))
    check("subscriber at fan-out/high-water boundary receives row exactly once",
          [e.get("id") for e in boundary_events].count(boundary_row) == 1)
    hub.unsubscribe(boundary_q)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
