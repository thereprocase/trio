# TODO — Trio

## Open

### Long-duration sentinel soak test
**Severity:** Medium | **Since:** v5.0 RC2 (2026-04-06)

Both sentinels validated in controlled testing. Need sustained observation (2+ hours) to verify:
- Agent context stays under 30K tokens after many loops
- Watchdog correctly catches message sentinel death in production
- Sleep mode persists correctly through extended idle periods
- No DB connection issues from the sentinel's long-lived connection

### Cadence rule enforcement gap
**Severity:** Medium | **Since:** v4.5 live test (2026-04-03)

Partially addressed by watchdog sentinel cadence detection (v5). The watchdog nags when cadence silence exceeds threshold. But the root cause — model-level attention drift during absorptive tasks — remains. The watchdog is the mechanical backstop, not the cure.

See `reviews/v45-live-test/TODO-cadence-escape.md`.

### Remove deprecated scripts
**Severity:** Low | **Target:** v6

`roam_hive_mind_wait.py` and `roam_hive_mind_watchdog.py` are deprecated. Sentinel subsumes both. Remove after confirming no sessions reference them directly.

### Conversation export quality
**Severity:** Low | **Since:** v4

The markdown export (`roam_hive_mind_end`) is functional but minimal. Task state changes aren't timestamped in the export. Lock acquisitions/releases aren't included.

## Completed (v5.0 RC2)

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
