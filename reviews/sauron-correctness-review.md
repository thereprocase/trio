### Sauron -- Opus

# Trio MCP Server v4 -- Correctness Review

**Scope:** server/trio_server.py (1688 lines), server/trio_wait.py (163 lines)
**Date:** 2026-04-03
**Focus:** Data flow integrity, state machine correctness, concurrency, coordinate systems, boundary conditions, lock system

---

## Critical

### 1. [severity: critical] trio_server.py:1293-1335 -- Lock acquisition has a TOCTOU race

The lock system reads the existing lock (line 1294-1297), checks expiry, then either deletes and re-inserts or returns conflict. Between the SELECT and the INSERT at line 1333-1336, another process could acquire the same lock. Two processes seeing an expired lock simultaneously would both DELETE then both INSERT -- but the SQLite PRIMARY KEY constraint on (channel, resource) means the second INSERT fails with an IntegrityError that is **uncaught**. This propagates as an unhandled exception.

Race timeline:
1. Process A: SELECT lock -> sees expired
2. Process B: SELECT lock -> sees expired
3. Process A: DELETE expired, INSERT new lock -> succeeds
4. Process B: DELETE (no-op, already gone), INSERT new lock -> IntegrityError

**Fix:** Wrap lines 1293-1344 in a BEGIN IMMEDIATE transaction, or use INSERT OR REPLACE with a conditional subquery, or catch sqlite3.IntegrityError on the INSERT and return a conflict response.

### 2. [severity: critical] trio_server.py:496-510 -- Blocked task creation race causes permanently stuck tasks

When creating a blocked task, the code checks whether all blockers are done (lines 497-503) and sets initial_status. Between this check and the INSERT at line 507, a blocker could be completed by another process, which triggers the unblocking logic in trio_complete (lines 987-1010). But the unblocking scan at line 987 looks for status=blocked -- the new task has not been inserted yet, so the scan finds nothing. The new task is then inserted with status=blocked and stays there permanently.

Race timeline:
1. Process A (trio_send with task=True, blocked_by=5): SELECT blockers -> task 5 not done, set initial_status=blocked
2. Process B (trio_complete for task 5): UPDATE task 5 to done, scan blocked tasks -> finds none
3. Process A: INSERT new task with status=blocked -> stuck forever

**Fix:** After inserting the blocked task, re-check whether all blockers are now done. If so, immediately update the task to open.

---

## Important

### 3. [severity: warning] trio_server.py:617-731 -- trio_poll holds database connection for up to 30 seconds

The polling loop holds db (from line 614) across the entire wait period, sleeping 2 seconds between iterations. Each iteration commits a heartbeat UPDATE. Under WAL mode other writers can proceed, but with 20 members all polling at wait_seconds=30, that is 20 persistent connections to the same SQLite file. Contrast with trio_wait.py (line 139-140) which properly closes and reopens on each cycle.

**Impact:** Scaling ceiling. Connection starvation possible under heavy load combined with the 10-second connection timeout.

### 4. [severity: warning] trio_server.py:1667-1677 -- trio_cleanup all_ended path does not delete locks

The per-channel cleanup path (line 1661) deletes locks. The all_ended loop (lines 1671-1677) omits this step. Ended channels accumulate orphaned lock rows in the database.

**Fix:** Add a DELETE FROM locks statement inside the all_ended loop at line 1673, before the tasks/messages/members/channels deletions.

### 5. [severity: warning] trio_server.py:524-541 -- Mention detection has substring collision

If member "Al" and member "Albert" both exist, a message containing @Albert matches both because @al is a substring of @albert (line 539 uses Python in operator for substring check). The @all broadcast at line 528 runs first so it does not cause a false name match, but inter-member substring collisions silently produce false mentions.

**Fix:** Match on word boundaries or exact whitespace-delimited tokens.

### 6. [severity: warning] trio_server.py:47-49 -- Member ID collision produces unhandled IntegrityError

generate_member_id() produces 6-character IDs (36^6 ~ 2.18 billion values). Collision probability per channel is negligible, but the INSERT at line 326 does not catch sqlite3.IntegrityError. A collision would crash the tool call.

**Fix:** Catch IntegrityError and retry with a new ID.

### 7. [severity: warning] trio_server.py:686-692 -- Watermark auto-advance comment claims backward compatibility but behavior differs

The comment at lines 679-683 says "auto-ack: advance watermark to where the PREVIOUS poll left off" but the code at line 687 advances to the max ID of the CURRENT unread set. This means the current poll's messages ARE acked immediately when from_name is not set -- contradicting the comment. The comment describes a two-phase approach (ack old, return new unacked) but the code acks everything in one shot. No message loss occurs, but the documented behavior does not match the implemented behavior.

---

## Minor

### 8. [severity: note] trio_server.py:764 -- trio_ack allows watermark beyond max message ID

through_id is not validated against actual message IDs. A buggy caller could set through_id=999999999, permanently skipping all future messages below that ID. By design for idempotency, but a footgun for incorrect callers.

### 9. [severity: note] trio_server.py:150-215 -- export_conversation silently swallows all exceptions

Line 214: except Exception: return None. Disk-full, encoding, or permission errors are invisible. The channel still ends -- export is best-effort. Acceptable but worth logging.

### 10. [severity: note] trio_server.py:56-141 -- Schema DDL runs on every tool call

Every get_db() invocation runs 5 CREATE TABLE IF NOT EXISTS statements and 4 ALTER TABLE migration attempts. On hot paths like trio_poll (2-second cycles), this adds unnecessary overhead. SQLite caches schema, so cost is low but nonzero.

### 11. [severity: note] trio_wait.py:63-68 -- Heartbeat update runs for culled members

If a member is culled (deleted from members table), the heartbeat UPDATE at line 64 affects zero rows silently. The script continues polling because local_hwm is already set (lines 109-116 only run once). Exits only on timeout or channel end. Harmless but wastes cycles.

### 12. [severity: note] trio_server.py:1087-1088 -- Release message reads from pre-update task row

After clearing claimed_by to NULL (line 1079-1083), line 1087 reads task["claimed_by"] from the row fetched before the UPDATE. Correct today but fragile -- a refactor that re-fetches the task after the UPDATE would lose the claimer name.

### 13. [severity: note] trio_server.py:317-321 -- Stale members consume channel capacity

Member count for MAX_MEMBERS check counts all non-culled members, including stale/disconnected ones. A channel with 20 members where 18 are stale blocks new joins until someone runs trio_cull. Design choice, not a bug.

---

## State Machine Analysis: Task Lifecycle

```
States: open, blocked, claimed, done

Transitions:
  open    → claimed   (trio_claim)
  claimed → done      (trio_complete, only by claimer)
  claimed → open      (trio_release by claimer, trio_cull by admin)
  blocked → open      (automatic when last blocker completes)
  blocked → claimed   REJECTED (trio_claim checks at line 868)
  done    → *         TERMINAL (no transitions out)
  open    → done      REJECTED (trio_complete requires claimed status)
```

**Coverage gaps (design choices, not bugs):**
- No cancel/delete transition. Tasks posted in error persist until channel cleanup.
- No manual unblock. Blocked task with stuck claimer requires: cull → claim → complete → auto-unblock.
- No reopen from 'done'. Prematurely completed tasks cannot be reopened.

---

## Concurrency Model Assessment

SQLite WAL mode with `busy_timeout=5000` and connection `timeout=10` provides adequate write serialization. Most apparent TOCTOU races are benign because the second writer sees the first's committed state in its UPDATE WHERE clause.

Two genuine concurrency bugs exist:
1. **Lock TOCTOU** (C1): Expired lock replacement hits unhandled IntegrityError
2. **Blocked task creation** (C2): Task permanently stuck in 'blocked' state

Both require sub-second timing. Low probability under normal load, but real correctness gaps.

---

## Assessment

Well-engineered for its purpose. The v4 watermark refactor correctly eliminated the original race. Peek-only design in `trio_wait.py` is clean. Lock system and task lifecycle mostly sound. The two concurrency bugs are real but require narrow timing windows.

**Verdict:** ISSUES FOUND (2 critical, 5 warning, 6 note)
