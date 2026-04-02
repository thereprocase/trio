### Legolas — Sonnet

**Findings:**

1. **[critical]** `trio_server.py:55-112` — `get_db()` runs 4x `CREATE TABLE IF NOT EXISTS` + 2x `PRAGMA` statements on every single call. Every tool invocation (trio_poll, trio_send, trio_status, etc.) pays this initialization tax. For `trio_poll` with a 15-second wait and 2-second sleep intervals, that's 1 `get_db()` call at entry plus a fresh connection opened on every loop iteration in `trio_wait.py`. SQLite schema introspection on every call is gratuitous overhead. **Fix:** Initialize schema once at server startup (module level), not on every connection.

2. **[critical]** `trio_server.py:458-522` — `trio_poll` is a blocking long-poll inside an MCP tool call. With `wait_seconds=15` (default) and `time.sleep(2)` intervals, this holds the MCP tool call open for up to 15 seconds. MCP servers running over stdio use a single thread; one blocked `trio_poll` stalls all other tool calls from all other MCP clients sharing this server instance. If N agents poll simultaneously, the sleep intervals serialize. **Fix:** Either make `trio_poll` non-blocking (return immediately with `no_new`) and let the caller handle retry, or use `trio_wait.py` as the long-poll mechanism exclusively.

3. **[critical]** `trio_wait.py:44-124` — Opens and closes a fresh SQLite connection on every 3-second poll iteration, including `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` each time. Over a 1-hour session (1200 iterations): 1200 connection opens, 1200 `PRAGMA` roundtrips, 1200 schema-skipping WAL mode switches. WAL mode is sticky (persists in the DB file) so re-setting it every iteration accomplishes nothing after the first call. **Fix:** Open one connection before the loop, close it after. Move `PRAGMA` setup to connection init only.

4. **[warning]** `trio_server.py:499-503` — Inside the `unread` branch of `trio_poll`, a second query runs `SELECT MAX(id) FROM messages WHERE channel = ?` to advance the watermark. This is redundant — the last element of the already-fetched `unread` list has the max id. **Fix:** `max_id = unread[-1]["id"]` — no extra query needed.

5. **[warning]** `trio_server.py:689-697` — `trio_status` iterates tasks and calls `_get_member()` (a full DB roundtrip) for each claimed task to resolve the claimer's name. With N claimed tasks this is O(N) queries in a loop. The claimer name is already stored in the `tasks` table as `claimed_by` (member_id), but the member name is only in the `members` table. **Fix:** One `JOIN` or one `IN (...)` fetch for all claimers rather than N individual lookups.

6. **[warning]** `trio_server.py:795-815` — `trio_list` uses correlated subqueries: `(SELECT COUNT(*) FROM members ...)` and `(SELECT COUNT(*) FROM messages ...)` per channel row. With C channels this is 2C subquery executions. No indexes exist on `members.channel` or `messages.channel` (the schema has no `CREATE INDEX` statements), so each subquery is a full table scan. As message history grows across many channels, this degrades to O(C × M) where M is total message rows. **Fix:** Add indexes on `messages(channel)` and `members(channel, active)`.

7. **[warning]** `trio_server.py:55-112` — No indexes declared anywhere in the schema. The hot queries — `WHERE channel = ? AND id > ?` on `messages`, `WHERE channel = ? AND id = ?` on `members` — rely on primary key lookups for `id` but do channel-filtered scans without a channel index. As message count grows, `WHERE channel = ? AND id > ? AND member_id != ?` (the unread query in poll) will scan all rows for a channel sequentially. **Fix:** `CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel, id)` and `CREATE INDEX IF NOT EXISTS idx_members_channel ON members(channel)`.

8. **[warning]** `trio_server.py:573-578` — In `trio_claim`, after the atomic UPDATE succeeds, a separate SELECT fetches the task description to include in the claim message. The task description was never read before the UPDATE because the optimistic-update pattern skips the pre-fetch. This is a minor extra roundtrip that could be eliminated by reading the description before the UPDATE (at no concurrency risk, since description is immutable). Minor but pointless.

9. **[note]** `trio_server.py:317-318` — `trio_connect` fetches `recent` with `ORDER BY id DESC LIMIT 10`, then calls `reversed(list(recent))` to flip it back to chronological. A single `ORDER BY id ASC` with a subquery or CTE would avoid the Python reversal, though at this scale it is immaterial.

10. **[note]** `trio_wait.py:43-44` — No maximum runtime / timeout. A background `trio_wait.py` process polling for a channel that was already ended before it started, or for a member that was never inserted, will loop indefinitely (the `channel_gone` path exits, but a race where the channel exists but has no matching member returns `channel_gone` correctly). Confirmed safe, but worth noting there is no wall-clock timeout for pathological cases.

11. **[note]** `trio_server.py:116-183` — `export_conversation` fetches all messages for a channel with no LIMIT. For a long-running channel this could be a large result set materialized into memory at `trio_end` time. Not a hot path, but worth noting for channels with thousands of messages.

---

**Summary:** The dominant cost is connection-per-call in `get_db()` with repeated schema initialization, compounded in `trio_wait.py` by opening and closing a connection on every 3-second poll iteration; a missing index on `messages(channel, id)` means the unread-messages query will degrade linearly with message volume.

**Verdict:** ISSUES FOUND (3 critical, 5 warning, 3 note)
