# v3 Test Session Post-Mortem

**Date:** 2026-04-02
**Channel:** `trio-test`
**Duration:** ~35 minutes (22:34–23:09 UTC)
**Participants:** 6 members (3 active, 3 stale ghosts)

## What We Tested

38-test matrix: 24 core use cases, 14 edge cases. Covered all MCP tools, error paths, and concurrency scenarios. Full results in `trio-v3-test-log.md`.

**Results:** 24/24 core PASS, 12/14 edge PASS, 2 edge not tested in-channel (member limit, confirmed by code inspection).

## Bugs Found and Fixed

| # | Bug | Severity | Fix |
|---|-----|----------|-----|
| 1 | `active` field never reflects liveness (always true after join) | HIGH | Computed from `last_seen` via `_is_member_active()` |
| 2 | No way to release tasks from dead sessions | HIGH | Added `trio_release` tool with stale-claimer check |
| 3 | MCP transport truncation is client-specific | LOW | Documented; server check retained as defense-in-depth |
| 4 | `trio_wait.py` killed by Bash 120s timeout → false wakes | MEDIUM | Added `--timeout` flag, clean `{"event": "timeout"}` exit |
| 5 | Negative `--timeout` values bypassed wait entirely | LOW | Clamped to `max(1, ...)` |

## Coordination Failures

### The Reassignment Incident

**What happened:** Gandalf (coordinator) reassigned Frodo's README task to Sauron after one 30-second poll returned `no_new`. Frodo was mid-edit — working, not chatting.

**Root cause:** Coordinator confused "not responding to messages" with "stale/gone." Did not check `trio_status` for Frodo's actual `last_seen`. Bypassed the 5-minute stale threshold and `trio_release` staleness guard — the exact tools designed to prevent this.

**Impact:** Near-clobber of Frodo's completed work. Frodo's README survived only because Sauron hadn't started yet.

**Lesson:** Silence ≠ absence. "Working, not chatting" is the default state during edits. Always check `trio_status` before reassigning. Always use `trio_release` (which enforces the stale threshold) instead of verbal reassignment.

**SKILL.md action:** Add rule: "Before reassigning someone's task, check `trio_status` for their `last_seen`. Only reassign via `trio_release` if they're past the stale threshold."

### The Three Saurons Problem

**What happened:** Three separate Sauron instances joined the channel (ae52u9, 1xbpvn, kmo3zq). Two went stale but still showed `active: true` (bug #1). One stale Sauron (ae52u9) woke up 13 minutes later and started issuing commands. Another (1xbpvn) claimed task #2 and then the fresh Sauron couldn't complete it.

**Root cause:** Bug #1 (active field always true) made it impossible to tell live from dead. No mechanism to release orphaned tasks (bug #2).

**Impact:** Confusion about which Sauron was authoritative. Duplicate or contradictory reports. Claimed task stuck on a dead session.

**Resolution:** Both bugs fixed. `_is_member_active()` computes liveness from heartbeat. `trio_release` allows recovery of orphaned tasks.

### The Two-Copy Problem

**What happened:** Edits landed in two different locations — `D:/ClauDe/tools/trio/` (git repo) and `C:/Users/exampleuser/.claude/skills/trio/` (skill install). Nobody knew which was canonical until mid-session.

**Root cause:** Undocumented workflow. The repo is the golden copy; the skill install is a release target. This wasn't stated anywhere.

**Impact:** Partial fixes in one copy, different fixes in the other. Required a manual merge and sync.

**Resolution:** Workflow clarified: edit the repo, copy to skill install for testing/release.

## What Worked Well

- **Atomic task claims** prevented duplicate work on the three test blocks
- **@mentions** got attention quickly and reliably (with `has_mentions` flag)
- **Test matrix in the shared log** was an effective coordination artifact
- **Trio itself held up** under a chaotic 6-member, 130+ message stress test with zero crashes or data corruption
- **SQLite WAL mode** handled concurrent access from multiple sessions without contention
- **The bugs we found were real** and the fixes are clean

## Participants

| Name | ID | Role | Status |
|------|----|------|--------|
| Frodo | tq0sfg | Original test Frodo | Stale (disconnected early) |
| Sauron | ae52u9 | Original Sauron | Stale (woke up once, confusing) |
| Frodo | l0j5rp | UX reviewer — tested edge cases, wrote README | Active throughout |
| Gandalf | bxfaln | Coordinator — test matrix, lifecycle tests, bug triage | Active throughout |
| Sauron | 1xbpvn | Tested task lifecycle, claimed task #2 | Stale (died mid-session) |
| Sauron | kmo3zq | Code fixes, review, trio_wait.py fix | Active throughout |
