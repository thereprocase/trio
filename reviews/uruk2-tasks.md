# Uruk-Hai #2 — Task Lifecycle Bug Hunt

**Focus:** Task state machine, claim atomicity, blocker dependencies, release logic
**Date:** 2026-04-03
**Verdict:** ISSUES FOUND (1 critical)

## Critical (1)

### Blocked tasks never unblock when blocker is released or culled

**File:** `server/trio_server.py:1080, 1597`

Tasks in "blocked" status remain permanently stuck when their blocker task is released via `trio_release()` or when the blocker's claimer is removed via `trio_cull()`. The unblock logic (lines 985-1010) only executes in `trio_complete()` when a blocker reaches "done" status.

**Scenario:**
1. Task A created (status=open)
2. Task B created with `blocked_by=[A]` (status=blocked)
3. Member claims A (status=claimed)
4. Member releases A via `trio_release()` (status→open)
5. Task B remains "blocked" — **will never unblock**

**Also triggered by:** `trio_cull()` on the member who claimed blocker task A (line 1597 releases A to "open" without unblock check).

**Root cause:** Unblock logic (lines 985-1010) only runs in `trio_complete()`. Neither `trio_release()` (line 1080) nor `trio_cull()` (line 1597) check for downstream blocked tasks.

**Impact:** Permanent task deadlock. No member can claim blocked tasks. Only manual DB cleanup fixes it.

**Fix:** After line 1080 in `trio_release()` and after line 1597 in `trio_cull()`, run the same unblock logic as `trio_complete()`.

## Clean Areas

- **Claim atomicity** (line 887): `UPDATE WHERE status='open'` ensures one winner
- **Complete atomicity** (line 959): `UPDATE WHERE status='claimed'` ensures only claimer completes
- **Self-release enforcement** (line 1070): `claimed_by` check prevents releasing others' tasks
- **Blocker validation** (lines 484-491): Task IDs verified to exist before creation
- **Task ID collisions**: Uses `cur.lastrowid`, no collision risk
- **Blocked task claim prevention** (lines 868-882): Blocked tasks cannot be claimed
