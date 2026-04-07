### Sauron — Opus

**Scope:** v5.1 sentinel architecture — state machine correctness, data flow integrity, timeout boundaries, heartbeat discovery implications. Files reviewed: `server/roam_hive_mind_sentinel.py`, `server/messenger-foreground.py`, `server/sentinel-foreground.py`, `SKILL.md` (lines 66-165), `test-log.md`.

---

**Findings:**

1. **[severity: warning]** `roam_hive_mind_sentinel.py:216-224` — **Heartbeat check is a dead letter.** The sentinel updates `last_seen` at lines 152-156 and then reads it back at line 217. `heartbeat_gap` will always be near-zero. The comment at lines 220-224 acknowledges this, claiming it catches "silent UPDATE failures" and "gap accumulated between restarts." But a silent UPDATE failure on SQLite with WAL mode is not a real failure mode — if the write fails, the `OperationalError` handler at line 278 catches it. The between-restarts gap argument is valid only for the 60-second window between the script's MAX_RUNTIME exit and the Haiku agent relaunching it. During that window, no sentinel is updating `last_seen`, so the *other* sentinel (watchdog) could detect it. But the sentinel checking *its own* heartbeat against a value it just wrote will never trigger. This check does no harm but is misleading — it looks like protection that is not.

2. **[severity: warning]** `roam_hive_mind_sentinel.py:258-263` — **Redundant `latest_own` query.** In active mode, the cadence check at line 259-263 re-queries `SELECT created_at FROM messages WHERE channel = ? AND member_id = ? ORDER BY id DESC LIMIT 1` — the same query already executed at lines 169-173 for sleep confirmation. In active mode, `sleeping_flag` is False so the sleep-confirmation block is skipped and `latest_own` from line 169 is stale or undefined. This is correct but wasteful. The variable name collision is a shadow risk: `latest_own` at line 169 is scoped to the `if sleeping_flag:` block but Python lacks block scoping, so if mode transitions from sleeping to active within the same loop iteration (impossible given the current structure, but a maintenance trap), the cadence check would use the wrong value. No bug today, but fragile.

3. **[severity: warning]** `roam_hive_mind_sentinel.py:95-101, 138, 149` — **`channel_ended` bypasses the watch filter unconditionally.** The `should_return()` gate is only used for `new_messages`, `flag_inconsistency`, and `cadence`. Terminal events (`channel_gone` at line 138, `error` at lines 129/280, `cap` at line 289) correctly bypass the filter. But `channel_ended` at line 149 *also* bypasses `should_return()` — it returns directly without consulting the watch list. So the `channel_ended` entry in both wrappers' `watch_events` lists is decorative. No bug (it would return either way), but misleading to anyone reading the watch_events arrays without also reading the sentinel's return logic.

4. **[severity: note]** `messenger-foreground.py:36` / `sentinel-foreground.py:30` — **MAX_RUNTIME is duplicated.** Both wrappers hardcode `MAX_RUNTIME = 3540`. If the bash timeout changes (e.g., for a Teams tier with a lower ceiling), both files must be updated in lockstep. The test-log's "Ideas / Future Work" section already identifies this. Flagging for completeness.

5. **[severity: note]** `roam_hive_mind_sentinel.py:110` — **`timeout=10` on DB connect vs `busy_timeout=5000` pragma.** The `sqlite3.connect(timeout=10)` controls how long Python waits to acquire the database *file lock* (10 seconds). The `PRAGMA busy_timeout=5000` controls how long SQLite waits when a table is locked by another writer (5 seconds). These are different mechanisms operating at different layers. Fine for a 59-minute connection lifetime, but could confuse a future maintainer.

6. **[severity: critical]** `roam_hive_mind_sentinel.py:116,284` — **The 60-second margin between MAX_RUNTIME and bash timeout is adequate but narrower than it appears.** The sentinel produces zero stdout for up to 59 minutes. The test log's own findings (lines 272-316) prove the bash timeout is an idle-output timer — stdout resets it, silence exceeding the timeout value kills the process. Since the sentinel prints nothing, the effective timeout is wall-clock from process start. `MAX_RUNTIME = 3540` and the script exits before 3600s of silence, so the margin is 60 seconds. However: the script sleeps for `check_interval` seconds (3s or 30s) at line 284, then performs DB queries. If a DB query blocks for the full `busy_timeout` (5s), one loop iteration can take up to 35 seconds. The deadline check at line 116 (`time.time() < deadline`) fires at the *top* of the loop, not after the sleep. A loop iteration starting at T=3539 will sleep 30s + query time, potentially not exiting until T=3574. That leaves 26 seconds of actual margin. This is sufficient, but the stated 60s margin is effectively 26s in the worst case. Worth documenting.

7. **[severity: warning]** `roam_hive_mind_sentinel.py:278-282` — **Transient DB errors are silently swallowed.** The `except sqlite3.OperationalError` catches everything except "no such table" and discards it with `pass`. This includes disk-full errors, corrupted WAL files, and permission changes. A sentinel running for 59 minutes against a corrupted DB would silently loop for the full runtime, producing no useful monitoring, and exit with a `cap` event as if nothing happened. The Haiku agent restarts it, and the cycle repeats indefinitely. The operator sees nothing. Correct for transient errors (WAL checkpoint contention), dangerous for persistent ones.

8. **[severity: note]** `SKILL.md:134,148` — **Event names `sentinel_loop_cap` and `watchdog_loop_cap` do not exist in the v5.1 architecture.** The wrappers convert `cap` to `{"event": "restart"}`, and Haiku loops on restart internally. The Haiku agent should never return a `cap` or `*_loop_cap` event to Opus. These table entries describe a scenario that cannot happen unless the Haiku agent exhausts its tool-call budget (which testing shows is effectively unlimited at 50+ calls). Misleading documentation.

9. **[severity: note]** `roam_hive_mind_sentinel.py:193-214` — **`local_hwm` initialized from `last_read` risks immediate re-notification.** On first loop iteration, `local_hwm` is set to `member["last_read"]` — the watermark from the last `ack` call. If messages exist between `last_read` and the current time that haven't been acked, the sentinel fires `new_messages` immediately on startup. This is probably correct (better to double-notify than miss), but means the messenger sentinel fires immediately after every restart if there are unacked messages. Opus relaunches, sentinel fires again, creating a hot loop until the ack completes. If the ack call fails, this becomes a livelock.

10. **[severity: warning]** `roam_hive_mind_sentinel.py:104,183-184,227-255` — **`prev_msg_count` survives mode transitions, enabling false-positive inconsistency events.** When `sleeping_flag` goes False (line 183-184), `inconsistency_streak` is reset to 0 but `prev_msg_count` is not. On the next transition back to sleeping, the delta between stale `prev_msg_count` and current count includes all messages sent during the active period, potentially triggering a false `flag_inconsistency` event. Concrete scenario: member sets sleeping (`prev_msg_count=50`), goes active, sends 10 messages, sets sleeping again. Watchdog sees `own_msg_count=61`, `msgs_sent=11 > 1`, increments streak. Two checks later: false positive. Partially mitigated by `send()` auto-clearing sleeping keywords on the server side, but the watchdog reads state at 30s intervals and can observe a window where the flag is re-set before the next poll.

---

**Summary:** The architecture is sound and well-validated by empirical testing, but the heartbeat self-check is a dead letter, silent DB error swallowing could mask persistent failures, and `prev_msg_count` state surviving mode transitions can produce false-positive inconsistency events.

**Verdict:** ISSUES FOUND (1 critical, 5 warning, 4 note)

---

**Recommendations:**

1. **Document the idle-output timer interaction in both wrapper scripts.** The 60s margin works, but the reasoning is non-obvious. A comment like `# Bash timeout is idle-output, not wall-clock. This script produces zero stdout, so effective timeout IS wall-clock. Worst-case loop overrun (~35s) means real margin is ~26s.` saves the next person 30 minutes of analysis.

2. **Add a consecutive-error counter to the DB error handler.** After N consecutive `OperationalError` exceptions (e.g., 10), return `{"event": "error", "msg": "Persistent DB failure after N retries"}`. Surfaces disk-full, corrupted WAL, or permission issues instead of silently looping for 59 minutes.

3. **Reset `prev_msg_count` on mode transitions.** When `sleeping_flag` goes False (line 183-184), also set `prev_msg_count = None`. Prevents stale deltas from triggering false inconsistency events after an active-then-sleeping-again cycle.

4. **Remove or rewrite the heartbeat self-check (lines 216-224).** Either delete it (it cannot trigger in normal operation) or restructure it to check *other members'* heartbeats, which is the actual watchdog use case. Each sentinel currently watches only its own member's heartbeat, which it just updated.

5. **Update SKILL.md event tables to remove `sentinel_loop_cap` and `watchdog_loop_cap`.** These events cannot reach Opus under v5.1. Replace with a note about what happens if the Haiku agent itself dies.

6. **Route `channel_ended` through `should_return()` for consistency, or remove it from both wrappers' `watch_events` lists with a comment.** The current state looks like a bug to anyone reading watch_events without also reading the sentinel return paths.

7. **The heartbeat discovery (idle-output timer with ~10x ratio requirement) does not affect the current architecture** because Option A avoids heartbeat keep-alive entirely. But if Option B is ever adopted, the sentinel's `check_interval` output cadence would need to actually print to stdout each cycle. Keep this as a documented constraint.

8. **Consider MegaSoak (4 hours, 4 restarts, 23K tokens) as the baseline reliability proof, not SoakTest (1 cycle, 59 min).** Future regression testing should target MegaSoak-equivalent durations.

---

Key files reviewed:
- `D:/ClauDe/tools/trio/server/roam_hive_mind_sentinel.py` — core sentinel loop, all findings trace here
- `D:/ClauDe/tools/trio/server/messenger-foreground.py` — message wrapper, findings 4 and 6
- `D:/ClauDe/tools/trio/server/sentinel-foreground.py` — watchdog wrapper, findings 4, 6, and 10
- `D:/ClauDe/tools/trio/SKILL.md` — agent prompts, finding 8
- `D:/ClauDe/tools/trio/test-log.md` — empirical evidence backing all findings
