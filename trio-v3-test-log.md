# Trio v3 Test Log — 2026-04-02

Channel: `trio-test`

## Test Matrix

### Core Use Cases
| # | Test | Owner | Status | Notes |
|---|------|-------|--------|-------|
| C1 | Connect — create new channel | Gandalf | PASS | Created `gandalf-test` scratch channel |
| C2 | Connect — join existing channel | Gandalf | PASS | Pippin joined `gandalf-test`; also confirmed on main channel |
| C3 | Connect — join ended channel (should fail) | Gandalf | PASS | Error: "Channel has ended" |
| C4 | Send — regular message | Gandalf | PASS | Proven by all channel messages |
| C5 | Send — empty message (should fail) | Sauron | PASS | Whitespace-only → "Message cannot be empty." |
| C6 | Send — message > 4000 chars (should fail) | Frodo+Sauron | PASS | Frodo got server's "4001 > 4000" error. Sauron's client truncated to 3085 — client-dependent, not server bug. Server check works. |
| C7 | Send — task=True creates claimable task | Gandalf | PASS | Tasks #2-#4 created via task=True |
| C8 | Send — pin=True sets channel objective | Gandalf | PASS | pin_topic on create auto-pins |
| C9 | Poll — returns unread messages from others | Gandalf | PASS | Received Frodo/Sauron messages |
| C10 | Poll — skips own messages | Gandalf | PASS | Own sends never returned in poll |
| C11 | Poll — wait_seconds=0 instant return | Gandalf | PASS | Used throughout testing |
| C12 | Poll — detects ended channel | Sauron | PASS | Rival polls after end → `{"event": "ended"}` |
| C13 | Claim — atomic claim succeeds | Gandalf | PASS | Claimed task #4 |
| C14 | Claim — conflict when already claimed | Sauron | PASS | Rival claims Sauron's task → `{"conflict": true}` |
| C15 | Complete — marks task done with result | Gandalf | PASS | Completed task #4 with result string |
| C16 | Complete — fails if not the claimer | Sauron | PASS | Rival tries to complete Sauron's task → "claimed by someone else" |
| C17 | Complete — fails if task already done | Sauron | PASS | Second complete → "Task #5 is already done." |
| C18 | Status — shows members, tasks, message count | Gandalf | PASS | Correct counts, objective, last_seen |
| C19 | End — closes channel, exports markdown | Gandalf | PASS | Ended `gandalf-test`, log exported |
| C20 | End — already-ended channel (should fail) | Gandalf | PASS | Error: "Channel already ended" |
| C21 | List — shows all channels | Gandalf | PASS | 3 channels with correct counts |
| C22 | Cleanup — delete ended channel | Gandalf | PASS | Deleted `gandalf-test` |
| C23 | Cleanup — refuses to delete active channel | Gandalf | PASS | Error: "still active" |
| C24 | Cleanup — all_ended=True bulk delete | Gandalf | PASS | Deleted `bulk-cleanup-test` |

### Edge Cases
| # | Test | Owner | Status | Notes |
|---|------|-------|--------|-------|
| E1 | Two members race to claim same task | Sauron | PASS | Sauron claims first, Rival gets conflict. Atomic via SQL WHERE status='open' |
| E2 | @mention detection — single member | Gandalf | PASS | Frodo @Gandalf → mentioned=true, has_mentions=true |
| E3 | @all broadcast mention | — | — | |
| E4 | Duplicate member names (two Frodos) | — | — | |
| E5 | Pin overwrites previous pin | Gandalf | PASS | Second pin replaced first in objective |
| E6 | Claim non-existent task ID | Sauron | PASS | task_id=999 → "Task #999 not found." |
| E7 | Complete unclaimed task (should fail) | Sauron | PASS | Complete open task → "not claimed yet. Claim it first." |
| E8 | Send to non-existent channel | Sauron | PASS | → "Channel not found." |
| E9 | Invalid channel code format | Gandalf | PASS | "INVALID_CODE!" → clear validation error |
| E10 | Poll with invalid member_id | Sauron | PASS | bogus member_id → "not a member of this channel" |
| E11 | Watermark advances correctly after poll | Sauron | PASS | 3-step: send→poll(gets msg 53)→send→poll(gets only msg 54)→poll(no_new) |
| E12 | Background wait script detects messages | — | — | |
| E13 | Member count limit (20 max) | — | — | |
| E14 | Export conversation to markdown on end | Gandalf | PASS | Correct metadata, roster, all messages |

## Member Inventory (as of 22:42 UTC)

| ID | Name | Status | Last Seen | Role |
|----|------|--------|-----------|------|
| `tq0sfg` | Frodo | ○ stale | 22:35 | Original test Frodo — disconnected |
| `ae52u9` | Sauron | ○ stale | 22:35 | Previous Sauron session — disconnected |
| `l0j5rp` | Frodo | ● active | 22:42 | Repro's UX reviewer — claimed task #3 |
| `bxfaln` | Gandalf | ● active | 22:42 | Architect — claimed task #4, built test matrix |
| `1xbpvn` | Sauron | ● active | 22:40 | This session — claimed task #2 |

**3 active sessions, 2 stale.** Gandalf is coordinating the test plan.

## Task Assignments

| Task | Description | Owner | Status |
|------|-------------|-------|--------|
| #1 | Task race (Frodo vs Frodo) | Frodo (`l0j5rp`) | claimed |
| #2 | Task lifecycle tests | Sauron (`1xbpvn`) | claimed |
| #3 | UX edge cases | Frodo (`l0j5rp`) | claimed |
| #4 | Channel lifecycle | Gandalf (`bxfaln`) | claimed |

## Session Log

### 22:34 — Channel created
- Frodo (`tq0sfg`) creates `trio-test`, first join message
- C1 (create new channel) confirmed

### 22:35 — Sauron (original) joins
- Sauron (`ae52u9`) joins, confirms comms working
- C2 (join existing) confirmed
- C4 (regular message send) confirmed
- C9 (poll returns unread) confirmed — Frodo's message delivered

### 22:39 — Frodo (Repro's) + Gandalf join
- Second Frodo (`l0j5rp`) connects — E4 (duplicate names) confirmed, two Frodos coexist
- Gandalf (`bxfaln`) connects, builds test matrix, posts tasks #2-#4
- C2 (join existing) confirmed again
- C7 (task creation via task=True) confirmed — tasks #1-#4 created
- C13 (claim success) confirmed — Frodo claimed #1 and #3, Gandalf claimed #4

### 22:40 — Sauron (this session) joins
- Sauron (`1xbpvn`) connects, claims task #2
- C13 confirmed again — atomic claim succeeded
- Note: stale members (`tq0sfg`, `ae52u9`) still show as "active" in member list
  - **Observation:** `active` field is set to 1 on join and never flipped to 0.
    Staleness is only detectable by comparing `last_seen` to current time.
    The `active` column is effectively a "joined" flag, not a heartbeat indicator.
    This could confuse consumers expecting `active=true` to mean "recently seen."

### 22:44 — Sauron task lifecycle tests (isolated channel `sauron-task-tests`)
- Created scratch channel with 2 members: Sauron-Test (`y0d56w`) + Rival (`rvpwco`)
- **C5 PASS** — whitespace-only message rejected
- **C6 INCONCLUSIVE** — MCP transport truncates messages before they hit the server's 4000-char check
- **C14 PASS** — Rival gets clean conflict response when claiming Sauron's task
- **C16 PASS** — Rival can't complete Sauron's task
- **C15 PASS** — Sauron completes task, result stored
- **C17 PASS** — Double-complete blocked: "already done"
- **E1 PASS** — Race resolved atomically (SQL WHERE status='open')
- **E6 PASS** — Nonexistent task ID gives clear error
- **E7 PASS** — Can't complete unclaimed task
- **E8 PASS** — Send to ghost channel gives clear error
- **E10 PASS** — Poll with bogus member_id rejected
- **E11 PASS** — Watermark advances exactly: msg53→poll→msg54→poll→no_new
- **C12 PASS** — After trio_end, Rival's poll returns `{"event": "ended"}`
- Ended and exported `sauron-task-tests` channel

### Bugs / Issues Found
1. **`active` column is misleading** — Set to 1 on join, never set to 0. Stale members still show `active: true`. The field name implies liveness but delivers "has ever joined." Fix: either rename to `joined` or add heartbeat-based staleness detection (e.g., set active=0 when last_seen > 5 min).
2. **C6 client-dependent** — Sauron's MCP client truncated to ~3085 chars, but Frodo's delivered 4001 chars intact and triggered the server error. Server check works; some clients may silently truncate. Not a server bug — downgraded from bug to observation.
3. **`ended_by` in poll response returns member_id, not name** — `trio_end` stores `member_id` in `ended_by` column, and `trio_poll`'s ended event returns it raw. All other user-facing outputs resolve IDs to names. Inconsistent.

### Observations (from code review)
- Channel code validation regex: `^[a-z0-9][a-z0-9\-]{0,31}$`
- Max message length: 4000 chars
- Max members: 20
- Poll blocks up to 30s max (clamped from wait_seconds)
- Watermark uses max(unread message IDs), not max(all channel IDs) — good, prevents skipping concurrent messages
- Task claim is atomic via SQL UPDATE WHERE status='open' — single-row update, no race window
- @mention detection is case-insensitive, matches against member names
- Export writes to `~/.claude/trio/conversations/<channel>.md`
- `claimed_by` stores member_id in tasks table, but `trio_status` resolves it to name for display
- Poll skips own messages (WHERE member_id != ?) — C10 inherently covered
- `trio_complete` checks: task exists, status=claimed, claimed_by=you — three-way guard
- `trio_wait.py` correctly filters own messages (line 96-98: `member_id != ?`) and advances watermark on read
- `trio_wait.py` and `trio_poll` can race on watermark — if both run concurrently, one may consume messages the other expected. Not a bug per se, but the skill instructions say to use background wait AND interleave peeks, which creates this window. In practice: false wakes are harmless (poll returns no_new), but a poll could steal messages from the background monitor's next cycle
- **Background monitor timeout bug**: `trio_wait.py` loops forever (`while True`) waiting for messages, but `Bash(run_in_background=true)` has a default 120s timeout. After 2 minutes with no messages, the process is killed and Claude gets a "completed" notification with no output — a false wake. The skill instructions say to restart the monitor after handling messages, but don't account for timeout-induced restarts. Fix options: (a) pass `timeout=600000` to Bash, (b) add a max-wait to the script that returns `{"event": "timeout"}` cleanly before the Bash timeout kills it

---

## v3 Smoke Tests — Post-Fix Verification (channel: `v3`, 23:16 UTC)

Fresh session with MCP reloaded. All v3 patches live.

### Participants
| Name | ID | Role |
|------|----|------|
| Sauron | gngyh3 | Code review, release tests |
| Frodo | 7evvpx | UX, pin, @mention tests |
| Radagast | q1vvii | Stale-release, timeout tests |
| Gandalf | — | @mention receiver (from trio-test) |

### Smoke Test Results

| # | Fix | Test | Owner | Result | Notes |
|---|-----|------|-------|--------|-------|
| 1 | Computed liveness | trio_status on trio-test shows stale members as active:false | Gandalf+Radagast | PASS | tq0sfg/ae52u9 (last seen 22:35) → false; recent members → true |
| 2a | trio_release: self | Claim task, release own task | Sauron | PASS | Task returned to open |
| 2b | trio_release: active guard | Try to release active member's task | Frodo+Radagast | PASS | Blocked: "claimed by X who is still active" |
| 2c | trio_release: stale release | Release task from member past 5-min threshold | Radagast | PASS | Blocked at 4:45 elapsed, succeeded at 5:16 elapsed. Threshold precise. |
| 3 | trio_wait --timeout | Run with `--timeout 5`, verify clean exit | Radagast | PASS | Exited after 5s with `{"event": "timeout"}` |
| 4 | @mentions | Send @Gandalf, verify mentioned:true in poll | Frodo→Gandalf | PASS | has_mentions:true, mentioned:true on receiver |
| 5 | Pin | Send pin=True, verify in trio_status | Frodo | PASS | Pinned message shows as objective |

### Verdict
All 5 bug fixes verified live. v3 is production-ready.

---

## v3.1 Changes (channel: `v3`, 23:25–23:32 UTC)

Design direction from Repro: tasks are permanently locked to their claimer. No auto-release based on staleness. User stays in the loop for all member removal.

### Changes

| # | Change | File | Description |
|---|--------|------|-------------|
| 1 | trio_release self-only | trio_server.py | Removed stale-threshold release path. Non-self attempts rejected with error pointing to trio_cull. Server-enforced, not behavioral. |
| 2 | trio_cull (new tool) | trio_server.py | Removes a member from a channel. Auto-releases their claimed tasks. User permission required — Claudes must NEVER call autonomously. |
| 3 | trio_wait read-only watermark | trio_wait.py | Background script no longer advances last_read. Only trio_poll (MCP) writes watermark. Eliminates race between concurrent consumers. |
| 4 | SKILL.md updates | SKILL.md | trio_cull docs, trio_release updated to self-only, user-consent rules for release and cull. |

### Commits
- `1a5899f` — v3.1.1 pushed to GitLab, synced to skill install

### Design Rationale
The Frodo reassignment incident proved "stale" ≠ "gone." A 5-minute threshold is arbitrary — sessions editing files look identical to crashed ones. The new model:
- `trio_release` = give up your own task (self-only, no authorization needed)
- `trio_cull` = user removes a dead member + frees their tasks (user-authorized)
