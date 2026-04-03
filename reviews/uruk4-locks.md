# Uruk-Hai #4 — Locks & Liveness Bug Hunt

**Focus:** Resource locks, liveness, culling
**Date:** 2026-04-03
**Verdict:** ISSUES FOUND (1 critical)

## Critical (1)

### `trio_cleanup` orphans locks in `all_ended` path
**File:** `server/trio_server.py:1673-1676`

Same bug as Uruk #1 found independently — when `all_ended=True`, lock records are not deleted. Single-channel path (line 1661) correctly includes lock deletion.

## Clean Areas

- **trio_lock:** Correctly implemented
- **trio_unlock:** Proper ownership check
- **Lock expiry boundaries:** Handled correctly
- **Member liveness computation:** Correct UTC-based staleness check
- **trio_cull:** Properly releases locks and tasks for culled member
- **trio_set_status:** No injection or overflow issues
- **trio_roster vs trio_status:** Consistent data
