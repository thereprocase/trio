# TODO — Trio

## Open

### Sonnet triage layer (v5.1)
**Severity:** Medium | **Since:** v5.0 RC2 (2026-04-06) | **Branch:** `v5.1-sonnet-triage`

A Sonnet agent sits between the message sentinel and the Opus parent. Instead of every `new_messages` event waking the parent, Sonnet reads the messages, decides if they need the parent's attention, and only escalates what's actionable (@mentions, task assignments, direct questions). Channel chatter between other members gets absorbed.

Three possible approaches:
1. **Triage agent** — Sonnet reads messages, filters noise, only wakes Opus parent for actionable items. Biggest token savings (~70% fewer Opus wake-ups).
2. **Sonnet as idle helper** — Switch idle helper sessions from Opus to Sonnet entirely. Follow-up questions ("which file was that?") don't need Opus reasoning.
3. **Context-aware watchdog** — Sonnet reads last few messages before firing cadence nag. If worker said "starting a 20-min build," suppress the nag.

**Estimated impact:** Triage approach could reduce Opus wake-ups from ~80/session to ~24, saving ~400K Opus tokens replaced by ~200K Sonnet.

### Long-duration sentinel soak test
**Severity:** Medium | **Since:** v5.0 RC2 (2026-04-06) | **Partially addressed:** v5.1 (2026-04-07)

v5.1 wrapper scripts validated through empirical timeout testing and restart architecture tests. Remaining verification:
- ~~Agent context stays under 30K tokens after many loops~~ (restart arch = each bash call is fresh, no accumulation)
- Watchdog correctly catches message sentinel death in production (needs live test)
- Sleep mode persists correctly through extended idle periods (needs live test)
- ~~No DB connection issues from the sentinel's long-lived connection~~ (restart arch = DB connection closed/reopened each cycle)
- Production soak test with real trio channel (2+ hours)

### Haiku sentinel reliability on idle channels
**Severity:** Medium | **Since:** v5.0 RC2 live test (2026-04-06) | **Largely resolved:** v5.1 (2026-04-07)

**Original issues and resolution:**

1. ~~**Haiku agents sometimes run the sentinel script as background bash.**~~ Simplified prompts with explicit "Do NOT use run_in_background: true" showed 100% foreground compliance across 10+ test runs (small sample). Wrapper scripts eliminate all flags from the command, reducing prompt complexity.

2. ~~**600s bash timeout too short for idle channels.**~~ Empirically tested: `timeout: 600000` IS a hard 600s kill. `timeout: 3600000` proven to work for 16+ minutes (unfakeable breadcrumb test). v5.1 wrapper scripts use MAX_RUNTIME=3540s with 3600000 bash timeout. Script exits cleanly 60s before timeout, Haiku restarts it. Idle channels now restart every ~59 min instead of every ~10 min.

3. ~~**Watchdog agents hit permission walls on repeated bash calls.**~~ Restart architecture reduces bash calls per sentinel from ~6/hr (old cap-restart pattern) to ~1/hr (one long foreground call per cycle). Permission exhaustion should no longer occur.

**Remaining risk:** `timeout: 3600000` tested on Claude Max 20x only. Claude Teams behavior untested. If Teams enforces a lower timeout, MAX_RUNTIME in wrapper scripts needs adjustment.

### Cadence rule enforcement gap
**Severity:** Medium | **Since:** v4.5 live test (2026-04-03)

Partially addressed by watchdog sentinel cadence detection (v5). The watchdog nags when cadence silence exceeds threshold. But the root cause — model-level attention drift during absorptive tasks — remains. The watchdog is the mechanical backstop, not the cure.

See `reviews/v45-live-test/TODO-cadence-escape.md`.

### Remove deprecated scripts
**Severity:** Low | **Target:** v6

`roam_hive_mind_wait.py` and `roam_hive_mind_watchdog.py` are deprecated. Sentinel subsumes both. Remove after confirming no sessions reference them directly.

### UserPromptSubmit hook as sentinel complement (~v10)
**Severity:** Low / Idea | **Since:** v5.1 (2026-04-07)

Inspired by Gas Town's `UserPromptSubmit` hook pattern (Yegge). A Claude Code hook on `UserPromptSubmit` could check the trio DB for unread messages at every turn boundary. If messages are pending, the hook returns `{"decision": "block", "reason": "You have N unread trio messages..."}` and Claude Code re-injects that as system context. Zero background agents needed for message detection at turn boundaries.

**NOT a replacement for sentinels.** The hook only fires between turns — if Opus is mid-tool-call for 30 seconds, messages queue. The sentinel detects messages within 3 seconds during active work. The hook would be a **complement**: sentinel for fast sub-turn detection, hook for guaranteed turn-boundary detection. Belt and suspenders.

**Why low priority:** The sentinel architecture (v5.1) already works for 4+ hours unattended. The hook adds a second detection layer but doesn't solve a problem we currently have. Worth revisiting when the sentinel pattern is battle-tested in production and we have a clear picture of what failure modes remain.

**Reference:** Steve Yegge's Gas Town (`github.com/steveyegge/gastown`) uses this pattern as its primary message injection mechanism. See `D:/ClauDe/tools/yegge/gastown/` and `D:/ClauDe/tools/trio/test-log.md` § "Gas Town" for analysis.

### Conversation export quality
**Severity:** Low | **Since:** v4

The markdown export (`roam_hive_mind_end`) is functional but minimal. Task state changes aren't timestamped in the export. Lock acquisitions/releases aren't included.

## Completed (v5.0 RC2 — War Council)

- [x] Shared SLEEPING_KEYWORDS constant (roam_constants.py)
- [x] idx_messages_channel_member index for sentinel queries
- [x] War Council: 3 criticals fixed, SKILL.md contradictions resolved
- [x] Dual-sentinel pattern (message + watchdog)
- [x] Watchdog emergency protocol (relaunch BOTH on fire)
- [x] Internal looping (cap/error handled in-agent, never surface to parent)
- [x] Explicit FOREGROUND instruction in agent prompts (Haiku backgrounding fix)
- [x] "Relaunch FIRST, process SECOND" rule

## Completed (v5.0 RC1)

- [x] Unified sentinel (merges wait + watchdog)
- [x] `status_changed_at` column for transition tracking
- [x] `send()` auto-clears sleeping keywords
- [x] Flag inconsistency detection (2-observation threshold)
- [x] Sleep confirmation (60s verified silence)
- [x] SKILL.md simplified (~120 lines removed)
- [x] setup.sh deploys sentinel

## Completed (v4.9)

- [x] Agent-based idle monitoring (95% token reduction)
- [x] Fix misleading watermark comment in `roam_hive_mind_poll`
- [x] Gitignore `settings.local.json` and `.env` files
- [x] Add `CLAUDE.md` project guide
