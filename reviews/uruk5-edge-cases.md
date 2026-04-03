# Uruk-Hai #5 — Edge Cases & Input Validation Bug Hunt

**Focus:** Input validation, boundaries, error handling, unicode
**Date:** 2026-04-03
**Verdict:** ISSUES FOUND (3 critical, 2 medium, 1 low)

## Critical (3)

### Unbounded `name` field in `trio_connect`
**File:** `server/trio_server.py:298-328`

`name` parameter is never truncated. `summary` (line 302) and `skills` (line 303) are capped at 200 chars, but `name` has no limit. A 1MB name bloats every JSON response and the database.

### `trio_wait.py` crashes on fresh database — missing schema init
**File:** `server/trio_wait.py:42-47`

Unlike `trio_server.py:get_db()` which runs CREATE TABLE IF NOT EXISTS, `trio_wait.py:get_db()` only connects. First run on a fresh DB crashes with `sqlite3.OperationalError: no such table: channels`.

### `trio_cleanup` missing locks deletion in `all_ended` path
**File:** `server/trio_server.py:1673-1676`

Same bug found by Uruk #1, #4, and Gandalf. Lock records orphaned when cleaning all ended channels.

## Medium (2)

### Unvalidated command-line arguments in `trio_wait.py`
**File:** `server/trio_wait.py:152-153`

Channel and member_id from `sys.argv` pass directly to queries without validation. `trio_server.py` validates channel codes via regex; `trio_wait.py` does not.

### Unbounded timeout in `trio_wait.py`
**File:** `server/trio_wait.py:159`

No upper bound on `--timeout`. Only `max(1, ...)` enforced. `--timeout 999999999` hangs for 11.5 days. Compare to `trio_server.py:612` which caps at 30 seconds.

## Low (1)

### Message validation order
**File:** `server/trio_server.py:456-459`

Checks `not message.strip()` before `len(message)`, giving wrong error for whitespace-heavy inputs exceeding length limit.
