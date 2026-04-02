### Frodo — Opus

**Findings:**

1. **[critical]** `server/trio_server.py:820-856` — `trio_cleanup` happily deletes an **active** channel if you pass it by name. There is no guard checking `status == 'ended'` before wiping. A user who fat-fingers a channel code (or a Claude session following instructions from a peer message) can nuke a live conversation mid-flight with zero warning and zero confirmation. The `all_ended` path correctly filters to ended channels, but the explicit-channel path does not. Fix: refuse to delete active channels, or require an explicit `force=True` parameter.

2. **[critical]** `server/trio_server.py:286-288` — The join message content includes `skills` verbatim from user input, which is untrusted peer data. SKILL.md correctly warns that "all channel content is untrusted," but the server itself injects raw user-supplied `skills` and `summary` into system-style `[joined]` messages with no length limit or sanitization. A malicious participant could stuff a 4000-char `skills` string into every join message. The `summary` and `skills` parameters on `trio_connect` have no max length — only `message` in `trio_send` is capped at 4000. Fix: cap `summary` and `skills` to reasonable lengths (e.g., 200 chars each).

3. **[warning]** `server/trio_server.py:600-663` — `trio_complete` is the only way to release a claimed task, and it requires the original claimer's `member_id`. If a participant crashes or disconnects permanently, their claimed tasks are stuck forever in `claimed` status with no way for anyone else to reclaim or release them. The README acknowledges this ("task remains claimed until manually released") but there is no manual release mechanism. The user is told a thing exists that does not. Fix: add a `trio_release(channel, member_id, task_id)` tool, or let `trio_claim` override stale claims (e.g., claimer not seen in 10+ minutes).

4. **[warning]** `server/trio_server.py:35-43` — `generate_channel_code` can produce collisions. If two users independently type `/trio image-processing`, the topic-to-slug path produces the same code and they silently end up in the same channel. This is arguably a feature (shared topics converge), but it is never explained to the user. Someone expecting a private channel for "image-processing" will be surprised when a stranger shows up. Fix: document this behavior clearly in SKILL.md, or append a short random suffix to topic-derived codes.

5. **[warning]** `SKILL.md:286` — The "handoff tasks" guidance says to use `trio_complete` with no work done, then post a message explaining why. But `trio_complete` marks the task as `done`, which is semantically wrong — the task was abandoned, not completed. Other participants scanning the task list see `done` and assume the work is finished. There is no `abandoned` or `released` status. Fix: add a release/abandon mechanism distinct from completion.

6. **[warning]** `server/trio_wait.py:44` — `poll_for_messages` runs an infinite loop with no timeout or max iterations. If the channel never gets a message and never gets ended, this script runs forever. On Windows especially, orphaned background Python processes accumulate silently. There is no `--timeout` flag. Fix: add a max lifetime (e.g., 30 minutes default) so stale watchers eventually die.

7. **[warning]** `server/trio_server.py:458-523` — `trio_poll` blocks the MCP server thread for up to 30 seconds with a `time.sleep(2)` loop. Since each Claude session runs its own MCP server instance, this blocks that session's entire MCP communication during the wait. If the user tries to use any other MCP tool while `trio_poll` is blocking, it queues behind the sleep. SKILL.md partially mitigates this by recommending `wait_seconds=0` for interleave peeks, but the default is 15 seconds. Fix: make the default `wait_seconds=0` and let callers opt into blocking, or document clearly that this blocks all MCP tools for the duration.

8. **[note]** `setup.sh:140-166` — The Python-inside-bash settings.json manipulation is fragile. If `settings.json` has trailing commas, comments, or is malformed JSON, the Python `json.load` will crash and the setup fails without a clear recovery message. The error would be a raw Python traceback. Fix: wrap in try/except with a human-readable message like "Your settings.json has invalid JSON — fix it manually or delete it to start fresh."

9. **[note]** `SKILL.md:57` — The background wait script path is hardcoded as `~/.claude/skills/trio/server/trio_wait.py`, but a Claude session following these instructions will try to run it literally with `~` in the path. On Windows (where this repo lives), `~` expansion depends on the shell. If Claude uses a Bash tool, it works. If it somehow uses cmd or PowerShell, it fails silently. The path should be documented as `$HOME/.claude/skills/trio/server/trio_wait.py` or the platform-native equivalent.

10. **[note]** `server/trio_server.py:466` — When `trio_poll` finds the channel gone, it returns `{"event": "channel_gone"}` with no guidance. The user (or Claude session) gets a terse event name and no suggestion of what happened or what to do. Was it deleted? Did it never exist? Fix: include the channel code and a message like "Channel was deleted or never existed. Run trio_list() to see available channels."

11. **[note]** `README.md:109` — "Tasks can be reclaimed" is stated in Design Principles, but there is no reclaim mechanism in the code. This is documentation claiming a feature that does not exist.

12. **[note]** `SKILL.md:19` — The `--rounds` flag is described as applying "per-participant" but there is no implementation anywhere — not in the server, not in the wait script, not in any CLI. This is a SKILL.md-only concept with no backing code. A Claude session that tries to honor `--rounds 5` has to invent its own counting logic, which means behavior varies by session.

**Summary:** The core messaging and task-claim workflow is solid, but there are two places where a user can lose data (deleting active channels, stuck claimed tasks with no release), and several spots where error feedback leaves the user stranded.

**Verdict:** ISSUES FOUND (2 critical, 5 warning, 4 note)
