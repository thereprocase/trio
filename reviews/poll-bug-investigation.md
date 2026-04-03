# Poll Bug Investigation — Pink (Task #35)

## Bug Report
Taskmaster reported: during long polls (`wait_seconds=30`), messages arrive and get "marked read" but the poll returns `"no_new"` after timeout. Happened at least twice (msgs #494 and #582 missed).

## Root Cause: Watermark Race Between `trio_poll()` and `trio_wait.py`

Both `trio_poll()` (MCP tool, trio_server.py:573) and `trio_wait.py` (background script, line 124) independently advance the `last_read` watermark in the `members` table. When both are running concurrently for the same member, they race.

### The Race Sequence

1. **Agent calls `trio_poll(wait_seconds=30)`** — enters the polling loop at trio_server.py:595.

2. **Agent also has `trio_wait.py` running in background** — polling the same DB every 3 seconds.

3. **A message arrives** while `trio_poll` is sleeping at line 685 (`time.sleep(2)`).

4. **`trio_wait.py` detects the message first** (its 3s poll cycle fires before trio_poll's 2s sleep ends). It reads the message, **advances `last_read`** to the message's ID (lines 124-128), and exits. The message content is written to the background task output file.

5. **`trio_poll` wakes from sleep**, loops back to line 596, re-fetches `member` — gets the **updated** `last_read` (already advanced by trio_wait.py past the new message).

6. **Query at line 635** finds no messages with `id > last_read` because the watermark was already moved. Returns nothing.

7. **Deadline passes** → trio_poll returns `{"event": "no_new"}`.

### Why the Agent Misses the Message

The message *was* delivered — to the background script's output file. But the agent was blocked waiting on `trio_poll`'s MCP return value. By the time `trio_poll` returns `"no_new"`, the agent interprets it as "nothing happened" and doesn't check the (already-completed) background task output.

The background task notification *did* fire, but if the agent was in a blocking MCP call, it couldn't process the notification until the call returned — at which point the `"no_new"` response overshadowed it.

### The Design Tension

The `trio_wait.py` comment at lines 117-122 acknowledges this:

```python
# Previous design left this to trio_poll (MCP) to avoid races, but in
# practice the MCP commit doesn't always persist before the next trio_wait
# launch, causing the cursor to stick and the same messages to replay
# indefinitely. Since Claude calls trio_wait and trio_poll serially, the
# race risk is negligible compared to the stuck-cursor bug.
```

The assumption "Claude calls trio_wait and trio_poll serially" is wrong for agents using `wait_seconds > 0` in trio_poll. The background script runs concurrently with the blocking poll.

## Who's Affected

Only agents using **both** `trio_poll(wait_seconds=N)` with N > 0 **and** a background `trio_wait.py` simultaneously. Agents using only the background script (most agents in this channel) are unaffected — they never call trio_poll with blocking waits.

Taskmaster was likely the only one doing blocking polls (`wait_seconds=15` or `wait_seconds=30`) while also running the background script, which explains why only he experienced the bug.

## Recommended Fixes (Pick One)

### Option A: Don't advance watermark in `trio_wait.py` (revert to old design)
Remove lines 123-128 in `trio_wait.py`. Let only `trio_poll()` advance the watermark. This was the original design, reverted due to a stuck-cursor bug. That bug should be re-investigated — it may have a simpler fix.

**Pro:** Eliminates the race entirely.
**Con:** May reintroduce the stuck-cursor bug.

### Option B: Don't use blocking `trio_poll` — always use `wait_seconds=0`
Update SKILL.md guidance to say: never use `trio_poll(wait_seconds=N)` with N > 0 when a background script is running. Use `wait_seconds=0` for instant peeks only.

**Pro:** No code change needed.
**Con:** Relies on agent behavior, not enforced.

### Option C: Make `trio_poll` detect the race
In `trio_poll`, after finding no unread messages and before returning `"no_new"`, check if `last_read` changed since the start of the poll. If it did, return a new event like `{"event": "read_elsewhere"}` to signal the agent to check other sources.

```python
# At start of poll, capture initial watermark
initial_last_read = member["last_read"]

# ... polling loop ...

# Before returning no_new:
member_now = _get_member(db, channel, member_id)
if member_now and member_now["last_read"] > initial_last_read:
    return json.dumps({"event": "read_elsewhere", 
                       "hint": "Messages were delivered to another reader (e.g. background script)"})
return json.dumps({"event": "no_new"})
```

**Pro:** Doesn't break existing behavior, informs the agent.
**Con:** Doesn't deliver the actual messages — just a hint.

### Option D: Locking — `trio_wait.py` skips watermark advance if a poll is active
Add a `polling_since` timestamp to the members table. `trio_poll` sets it on entry, clears on exit. `trio_wait.py` checks it — if a poll is active, skip the watermark advance (still return the messages to the output file).

**Pro:** Both paths work correctly.
**Con:** More complex, needs schema change.

## My Recommendation

**Option A** (revert trio_wait.py watermark advance) is the cleanest fix. The stuck-cursor bug it was meant to fix deserves a proper investigation — it likely has a root cause in MCP response timing, not in watermark ownership. The watermark should have a single writer.

If Option A reintroduces the stuck-cursor, then **Option C** (detect-and-hint) is the safest fallback — no behavior changes, just better diagnostics.
