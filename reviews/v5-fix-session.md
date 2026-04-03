# Trio v5 Fix Session — 2026-04-03

## How We Got Here

Trio is a multi-participant async communication MCP server for Claude Code sessions. It reached v4 through a live 8-agent session that identified and fixed a watermark race condition, added resource locks, member status, poll filtering, and external roster. But v4 never had an independent third-party code review.

This session deployed 7 reviewers in parallel:
- **Gandalf** (Opus) — architecture and API design
- **Sauron** (Opus) — correctness, state machines, concurrency
- **5 Uruk-hai** (Haiku) — targeted bug hunting across connections, tasks, messaging, locks, and edge cases

## What They Found (Deduplicated)

6 critical bugs, 5 warnings, and minor notes. The `trio_cleanup` locks bug was found independently by 4 reviewers — high confidence it's real and obvious.

## Fix Plan

Each fix below explains: (1) why the code ended up this way, (2) what's wrong, and (3) how we're fixing it.

---

### C1. Lock TOCTOU Race (`trio_server.py:1293-1335`)

**Why it ended up this way:** The lock system was added in v4 during a live session. The author followed the natural read-check-write pattern: SELECT existing lock, check if expired, DELETE if yes, INSERT new lock. Under single-process conditions this is correct. The author was likely focused on the expiry and conflict logic and didn't consider that two MCP server instances share the same SQLite database.

**What's wrong:** Two processes can both see an expired lock, both DELETE it (second DELETE is a no-op), and both INSERT. The second INSERT hits the PRIMARY KEY constraint `(channel, resource)` and throws `sqlite3.IntegrityError`. This exception is uncaught, so the MCP tool call crashes instead of returning a conflict response.

**Fix:** Catch `sqlite3.IntegrityError` on the INSERT at line 1333 and return a conflict response. This is cheaper than `BEGIN IMMEDIATE` (which adds latency to every lock call even without contention) and correctly handles the race: if your INSERT fails, someone else won the lock.

**Status:** Not yet implemented.

---

### C2. Blocked Task Creation Race (`trio_server.py:496-510`)

**Why it ended up this way:** The blocked_by feature was added in v3.2. The author correctly checks whether all blockers are done before setting `initial_status`. The gap is between this check (line 496-503) and the INSERT (line 507-510). In a single-process world, nothing changes between those two lines. But with concurrent processes, a blocker can complete in another process between the check and the insert.

**What's wrong:** Process A checks blockers, sees task #5 is not done, sets `initial_status='blocked'`. Between the check and the INSERT, Process B completes task #5 and runs the unblock scan — but Process A's task hasn't been inserted yet, so the scan finds nothing. Process A inserts the task as 'blocked', and it stays blocked forever because the unblock event already fired and missed it.

**Fix:** After the INSERT, re-check whether all blockers are now `done` (or `cancelled`, once we add that state). If so, immediately UPDATE the task to 'open'. This is safe because it only unblocks when all dependencies are genuinely resolved — no false positives.

**Status:** Not yet implemented.

---

### C3. No Way to Cancel Tasks / Unblock Stuck Dependencies

**Why it ended up this way:** The task state machine was designed with a simple lifecycle: open → claimed → done, with release back to open. The `blocked_by` feature assumed blockers would always eventually complete. The v4 session didn't encounter a scenario where a blocker got permanently stuck (everyone was present and active).

**What's wrong:** If a blocker task is released (owner gives up) or its owner is culled (disconnected), the blocker goes back to `open`. Downstream blocked tasks check for `status == 'done'` on all blockers before unblocking. Since the blocker is now `open` (not `done`), the downstream tasks stay `blocked` forever. The only recovery is: someone claims the blocker, completes it, then auto-unblock fires. If nobody completes it, the entire downstream chain is stuck.

**Important nuance:** The naive fix proposed by Uruk-hai #2 (run unblock logic on release/cull) is WRONG. It would unblock downstream tasks even though the blocker work was never completed. This could cause agents to start work whose prerequisites aren't actually done.

**Fix:** Add a `trio_cancel` tool with a new terminal state `cancelled`. When a task is cancelled:
- It records a reason for the cancellation
- The unblock logic (in `trio_complete` and in the new `trio_cancel`) treats `cancelled` as a resolved dependency — same as `done` for unblocking purposes
- The coordinator decides when to cancel based on the actual situation: maybe the work is no longer needed, maybe it's being restructured, maybe the owner disappeared
- `trio_claim` blocked-check updated to consider `cancelled` as resolved
- Any member can cancel any task in `open` or `claimed` status (it's a coordinator action, not a claim-ownership thing)

The key insight is: cancellation is an explicit decision by the coordinator that says "this dependency is no longer blocking." It's not an accident (like release) or an infrastructure event (like cull). The coordinator takes appropriate action based on the real needs of the task network.

**Status:** Not yet implemented.

---

### C4. `trio_ack` Accepts Unbounded `through_id` (`trio_server.py:764`)

**Why it ended up this way:** `trio_ack` was added in v4 as part of the watermark race fix. The focus was on the ack/poll separation, not on validating the ack target. The `through_id <= current` check (idempotency guard) was included, but no upper bound check was added. The developer likely assumed callers would only pass IDs they received from `trio_poll`.

**What's wrong:** An agent can call `trio_ack(through_id=9999)` when only messages 1-3 exist. The watermark jumps to 9999, and all future polls return nothing because `WHERE id > 9999` matches nothing. Messages are permanently lost with no recovery.

**Fix:** Before advancing, validate `through_id <= max(message.id)` for the channel. Return an error if the ID is beyond the actual message range.

**Status:** IMPLEMENTED. Added validation query before the UPDATE.

---

### C5. Unbounded `name` Field in `trio_connect` (`trio_server.py:298`)

**Why it ended up this way:** The author capped `summary` and `skills` at 200 characters (lines 302-303) but overlooked `name`. Likely because names are "obviously short" — except there's no enforcement, and a malicious or buggy caller could pass a megabyte string that bloats every JSON response and database query.

**What's wrong:** No length limit on the `name` parameter. A very long name bloats all responses that include member lists (connect, status, roster, poll messages).

**Fix:** Cap `name` at 50 characters. Names should be shorter than summaries — they're display labels, not descriptions.

**Status:** IMPLEMENTED. Added `name = name[:50]` after the default name generation.

---

### C6. `trio_wait.py` Crashes on Fresh Database

**Why it ended up this way:** `trio_server.py:get_db()` creates all tables on every call. `trio_wait.py` was written as a lightweight companion script that connects directly to SQLite without the schema-creation overhead. The assumption was that the MCP server would always run first and create the tables. This is true in practice — you connect before you wait — but if someone runs `trio_wait.py` on a machine where the database doesn't exist yet, it crashes.

**What's wrong:** `trio_wait.py:get_db()` (lines 42-47) connects without creating tables. If the DB file doesn't exist or is empty, the first query hits `sqlite3.OperationalError: no such table`.

**Fix:** Wrap the initial channel query in a try/except that returns a clear error message instead of crashing. We won't duplicate the full schema creation — that's the server's job — but we'll fail gracefully.

**Status:** Not yet implemented.

---

### W1. `trio_cleanup` Missing Lock Deletion in `all_ended` Path (`trio_server.py:1673-1676`)

**Why it ended up this way:** The single-channel cleanup path (line 1661) was written first and correctly includes lock deletion. The `all_ended` path was added later (or written quickly) and the author copied the delete sequence but missed the locks table. Classic copy-paste omission — the single-channel path has 5 DELETE statements, the all_ended path has 4.

**What's wrong:** Orphaned lock records accumulate in the database. They reference channels and members that no longer exist.

**Fix:** Add `db.execute("DELETE FROM locks WHERE channel = ?", (code,))` inside the all_ended loop, before the other DELETEs.

**Status:** Not yet implemented.

---

### W3. Mention Detection Substring Collision (`trio_server.py:539`)

**Why it ended up this way:** The mention detection uses Python's `in` operator for substring matching: `if f"@{name.lower()}" in content_lower`. This is the simplest possible implementation and works perfectly when all names are unique and no name is a prefix of another. The author probably didn't consider the "Al" vs "Albert" case.

**What's wrong:** If member "Al" and "Albert" both exist, `@Albert` triggers a mention for both because `@al` is a substring of `@albert`.

**Fix:** Use a regex with word boundary: `re.compile(r"@" + re.escape(name) + r"(?:\b|$)", re.IGNORECASE)`. This ensures `@Al` only matches when followed by a word boundary or end of string, not when it's a prefix of a longer name.

**Status:** IMPLEMENTED.

---

### W4. Member ID Collision Unhandled (`trio_server.py:325, 350`)

**Why it ended up this way:** 6-character alphanumeric IDs have 2.18 billion possible values. The probability of collision per channel is negligible. The author reasonably decided not to add collision handling for something that would happen roughly once every billion joins. But "negligible" is not "zero," and an unhandled crash is worse than a retry.

**What's wrong:** If a collision occurs (astronomically unlikely but possible), the INSERT throws `sqlite3.IntegrityError` which propagates uncaught.

**Fix:** Catch `IntegrityError` on the member INSERT and retry once with a new ID. One retry is sufficient — two consecutive collisions is beyond negligible.

**Status:** IMPLEMENTED (both join-existing and create-new paths).

---

## Implementation Order

1. ~~C5 — name cap~~ ✓
2. ~~W4 — member ID collision retry~~ ✓
3. ~~W3 — mention word boundary~~ ✓
4. ~~C4 — trio_ack bounds~~ ✓
5. ~~C1 — lock TOCTOU catch~~ ✓
6. ~~C2 — blocked task creation re-check~~ ✓
7. ~~C3 — trio_cancel (new tool, new state, unblock logic update)~~ ✓
8. ~~C6 — trio_wait graceful error~~ ✓
9. ~~W1 — cleanup locks in all_ended~~ ✓

All 9 fixes implemented. Both files parse clean.

## Where We're Going

After all fixes are implemented, this becomes v5. The main additions:
- `trio_cancel` tool — new task state for coordinator-driven dependency resolution
- Hardened concurrency in locks and task creation
- Input validation tightened across the board
- `trio_wait.py` made more robust

The SKILL.md and README.md will need updates to document `trio_cancel` and the new `cancelled` task state.
