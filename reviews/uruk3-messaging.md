# Uruk-Hai #3 — Messaging & Watermark Bug Hunt

**Focus:** Messaging, watermarks, polling
**Date:** 2026-04-03
**Verdict:** ISSUES FOUND (1 critical)

## Critical (1)

### `trio_ack` accepts arbitrary `through_id` — permanent message loss

**File:** `server/trio_server.py:764-772`

`trio_ack()` accepts arbitrary `through_id` values without validating they correspond to existing messages. An agent can ack to ID 9999 when only messages 1-3 exist, permanently skipping all unread messages.

**Scenario:**
1. Channel has messages 1, 2, 3. Member watermark is 0.
2. Member calls `trio_ack(..., through_id=9999)`
3. Validation at line 764: `if 9999 <= 0:` → FALSE
4. Watermark advances to 9999 (lines 767-770, no validation)
5. Next poll: `SELECT ... WHERE id > 9999` → returns nothing
6. Messages 1, 2, 3 lost forever

**Fix:** Validate `through_id <= max(message.id)` before advancing watermark.

## Clean Areas

- **trio_poll:** Auto-ack logic (lines 686-692) correctly advances to max of current unread
- **trio_wait.py:** Correctly does NOT advance watermark (v4 peek-only design)
- **trio_history:** Read-only, no watermark interaction
- **Message filtering:** from_name filter correctly skips watermark advance when no matches
- **Poll-between-ack race:** Already fixed per CHANGELOG commit 3855294
