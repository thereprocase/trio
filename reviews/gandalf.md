### Gandalf — Opus

I have walked through every corridor of this codebase. It is modest in size — two Python files, a setup script, a skill definition, and documentation. Let me tell you what I found.

**Findings:**

1. **[warning]** `server/trio_server.py:56-112` — Schema bootstrapped on every `get_db()` call. Every single tool invocation runs six DDL statements (`CREATE TABLE IF NOT EXISTS` x4 plus two PRAGMAs). This is harmless at small scale but wasteful. The PRAGMAs are fine — WAL mode and busy_timeout should be set per-connection. But the DDL should run once at startup or be guarded by a module-level flag. At 9 tools x N participants polling every 3 seconds, this is a lot of redundant DDL parsing.

   *Suggestion:* Initialize schema once at module load. Guard with a file-exists check or a module-level `_initialized` flag.

2. **[warning]** `server/trio_server.py:46` and `server/trio_wait.py` — Member IDs are 6-character random alphanumeric strings (`[a-z0-9]{6}`). That is 2.18 billion combinations — sufficient for the stated use case of <= 20 members per channel. But there is zero collision detection. `generate_member_id()` is called once in `trio_connect` and the result is inserted directly. A collision would silently corrupt the `(id, channel)` primary key constraint (SQLite would reject the INSERT, but the error surfaces as a generic failure, not a "please retry" message). The probability is low but the failure mode is confusing.

   *Suggestion:* Either catch `IntegrityError` on member INSERT and retry with a new ID, or use a longer ID (8+ chars).

3. **[warning]** `server/trio_server.py:458-523` — `trio_poll` holds a database connection open for up to 30 seconds in a `while True` loop with `time.sleep(2)`. SQLite connections are cheap, but holding one open while sleeping means the WAL checkpoint cannot reclaim that reader's snapshot. Under sustained polling by multiple participants, the WAL file grows without bound until everyone disconnects. The wait script (`trio_wait.py:43-124`) gets this right — it opens and closes the connection on each iteration.

   *Suggestion:* Match the pattern in `trio_wait.py` — close and reopen the connection on each poll iteration inside `trio_poll`.

4. **[warning]** `server/trio_server.py:551-556` — The atomic claim relies on `UPDATE ... WHERE status = 'open'` and checks `rowcount`. This is correct for SQLite's default serializable isolation within a single connection. But with WAL mode and multiple concurrent connections (multiple MCP server instances), two concurrent `UPDATE` statements can both see `status = 'open'` before either commits. SQLite's write lock will serialize them, so only one `UPDATE` will actually match — this is safe. But the code has no explicit transaction boundary around the claim. If `autocommit` behavior changes (e.g., moving to a connection pool or different DB), this atomicity guarantee evaporates.

   *Suggestion:* Wrap the claim in an explicit `BEGIN IMMEDIATE` transaction to make the atomicity guarantee self-documenting and portable.

5. **[note]** `server/trio_server.py:835-856` — `trio_cleanup` deletes in the order tasks -> messages -> members -> channels, which respects foreign key dependencies. But foreign keys are never enabled (`PRAGMA foreign_keys = ON` is absent). This means the FK declarations in the schema are purely documentary — SQLite ignores them by default. This is fine as long as cleanup always follows this delete order, but it means the schema lies about its constraints.

   *Suggestion:* Either add `PRAGMA foreign_keys = ON` to `get_db()`, or remove the `FOREIGN KEY` clauses to avoid false confidence. I lean toward enabling them.

6. **[note]** `server/trio_server.py:118-183` — `export_conversation` catches all exceptions and returns `None`. This is the only place in the codebase with a bare `except Exception`. If export fails (disk full, permissions, encoding), the user gets `"log_file": null` with no indication of why. The channel is already ended at this point, so the data is still in SQLite, but the user has no signal to retry.

   *Suggestion:* Log or return the exception message so the caller knows export failed and why.

7. **[note]** `server/trio_server.py:492` — Poll filters out the caller's own messages (`AND member_id != ?`) but advances the watermark to `MAX(id)` across all messages including the caller's own. This is correct — it prevents you from re-reading your own messages. But it means if you send a message and immediately poll, you skip your own message (good) and advance past it (good). The logic is sound but non-obvious. A comment explaining the asymmetry would help future maintainers.

8. **[note]** `server/trio_wait.py` — The wait script duplicates significant logic from the server: DB connection setup, watermark advancement, heartbeat updates, message formatting. If the schema or watermark logic changes in the server, the wait script must be updated in lockstep. There is no shared module between them.

   *Suggestion:* Extract shared DB access (connection setup, watermark queries, heartbeat update) into a `trio_db.py` module imported by both. This is the single largest maintainability risk I see — two files with identical logic that can drift.

9. **[note]** `SKILL.md` and `README.md` — Documentation quality is high. The skill file is thorough: argument parsing, security warnings about untrusted peer content, background monitoring requirements, dashboard rendering format, and worked examples. The README covers installation, tools, workflow, design principles, and limitations. Both are accurate reflections of the code. Well done.

10. **[note]** `setup.sh:140-166` — The allowlist injection uses inline Python with shell variable interpolation to modify `~/.claude/settings.json`. This works but is fragile — if `settings.json` contains characters that break the Python string interpolation (the path is injected as `'$SETTINGS_JSON'`), it fails silently. On Windows paths with backslashes or special characters, this could misfire.

    *Suggestion:* Pass the settings path as a command-line argument to the inline Python rather than embedding it in a string literal.

11. **[note]** Extensibility assessment for the requested features:
    - **@mentions:** Straightforward. Parse `@name` in message content, add a `mentions` column or a junction table. No schema changes needed if you just parse at read time.
    - **Pinned messages:** Add a `pinned` boolean to `messages` table, a `trio_pin` tool. Minimal.
    - **Message dedup:** Harder. Would need content hashing + a unique constraint. The current design has no dedup — sending the same message twice creates two rows. A `content_hash` column with a unique constraint per `(channel, member_id, content_hash)` within a time window would work.
    - **Stale claim recovery:** The README acknowledges this gap. Add a `claimed_at` timestamp to tasks (currently only `updated_at` serves this role). A `trio_reclaim` tool or a periodic sweep in `trio_poll` could release tasks claimed more than N minutes ago by members whose `last_seen` is stale. Medium effort, high value.

**Summary:** A clean, well-documented MCP server with sound fundamentals — the async model, atomic claims, and watermark polling are all correctly implemented. The primary risks are the duplicated logic between server and wait script (drift hazard), the long-held connection in `trio_poll`, and the undocumented atomicity assumptions around task claims.

**Verdict:** ISSUES FOUND (0 critical, 4 warning, 7 note)
