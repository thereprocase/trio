# Uruk-Hai #1 — Connection & Channel Bug Hunt

**Focus:** Connections, channels, cleanup, ending
**Date:** 2026-04-03
**Verdict:** ISSUES FOUND (2 critical, 1 high)

## Critical (2)

### `trio_end` race condition with simultaneous end calls
**File:** `server/trio_server.py:1498-1503`

Two members calling `trio_end()` simultaneously can both read `status == "active"` before either writes. Both UPDATEs succeed, second overwrites first's `ended_by` value.

**Scenario:**
1. Member A reads channel status = "active" (line 1491)
2. Member B reads channel status = "active" (line 1491)
3. A's UPDATE sets ended_by = A
4. B's UPDATE sets ended_by = B (overwrites)
5. Channel records B as ender, even though A called first

**Root cause:** DEFERRED transaction + snapshot isolation. SELECT doesn't acquire a lock.

### `trio_cleanup` missing locks deletion in `all_ended` path
**File:** `server/trio_server.py:1673-1676`

When `all_ended=True`, deletes tasks, messages, members, channels — but NOT locks. Single-channel path (line 1661) correctly deletes locks. Orphaned lock records accumulate.

## High (1)

### `trio_connect` unhandled IntegrityError on member_id collision
**File:** `server/trio_server.py:305, 325-329, 350-352`

Generates random 6-char member_id with no collision check. If collision occurs (probability ~1 in 2.2B but non-zero), INSERT fails with IntegrityError. No except clause catches it — exception propagates uncaught.
