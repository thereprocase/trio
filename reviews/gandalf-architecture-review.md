# Gandalf — Architecture Review of Trio MCP Server

**Reviewer:** Gandalf (Opus)
**Date:** 2026-04-03
**Verdict:** ISSUES FOUND (1 critical, 10 warning, 15 note)

## Files Reviewed

- `server/trio_server.py` (1688 lines — entire server)
- `server/trio_wait.py` (163 lines — background poller)
- `setup.sh` (installation script)
- `README.md` (user docs)
- `SKILL.md` (skill definition)
- `CHANGELOG.md` (version history)

## Critical (1)

### C1. TOCTOU race in `trio_lock` (`trio_server.py:1294-1337`)

Two processes can both see no existing lock and both INSERT, or both see an expired lock and both race to replace it. The PRIMARY KEY constraint causes the second INSERT to fail with `UNIQUE constraint failed`, which is **uncaught**.

**Fix:** Use `INSERT OR IGNORE` + rowcount check, or `BEGIN IMMEDIATE` before the SELECT.

## Warnings (10)

### W1. DDL runs on every `get_db()` call (lines 56-141)
Schema creation (CREATE TABLE IF NOT EXISTS) executes hundreds of times per minute under load. Should initialize once or gate behind a flag.

### W2. `trio_poll` holds connection open across 30-second blocking loop (lines 616-732)
The blocking poll loop keeps a SQLite connection open for the entire wait_seconds duration, preventing WAL checkpoints and increasing lock contention.

### W3. `trio_history` unbounded when `from_id` is set (lines 803-807)
No LIMIT clause when `from_id` is provided — could return the entire message log and exhaust memory.

### W4. `trio_cleanup` skips lock deletion in `all_ended` path (lines 1667-1677)
When cleaning all ended channels, orphaned rows remain in the locks table.

### W5. `export_conversation` swallows all exceptions silently (lines 214-215)
Export failures are caught and suppressed — channel ends but the user never knows the export failed.

### W6. README documents `trio_release` allowing cross-member release, but code enforces self-release only
Documentation/code mismatch. README says stale members' tasks can be released by others; code rejects it.

### W7-W10. (Additional structural warnings from review)
- Connection pooling absent — new connection per tool call
- No index on messages(channel, id) for poll queries
- No index on tasks(channel) for status queries
- Lock TTL check uses string comparison on ISO timestamps (works but fragile)

## Notes (15)

Structural debt and documentation consistency items — mostly cosmetic or minor efficiency improvements. Not blocking.

## What Works Well

- **Watermark race fix** is genuinely good engineering
- **Atomic task claims** use the correct optimistic concurrency pattern
- **Untrusted-peer-content** warnings show defensive thinking
- **CHANGELOG** recording rejected proposals and design principles is institutional memory done right
