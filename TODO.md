# TODO — Trio

## Open

### Sentinel live validation (14-point test plan)
**Severity:** High | **Since:** v5.0 RC1 (2026-04-06)

Gandalf's test plan for the unified sentinel. Tests 1-11 can run in one session. Tests 12-14 need sustained observation.

1. Message detection (active mode) — send while sentinel runs, expect return < 6s
2. Message detection (idle mode) — send while idle, expect return < 60s
3. Heartbeat nag (active) — stop polling 3min, expect heartbeat nag
4. Heartbeat nag (sleep) — stop polling 6min with idle status, expect wide nag
5. Cadence nag (active) — work without posting 4min, expect cadence nag
6. Cadence skip (sleep) — silent 10min with "standing by", no cadence nag
7. Flag inconsistency — set "idle" then send messages, expect nag
8. Sleep confirmation timeout — set "idle", send at 30s, expect SLEEP_PENDING
9. Sleep confirmation success — set "idle", wait 70s silent, confirm SLEEPING
10. Cycle cap — run with --max-runtime 120, expect cap event
11. Channel ended — end channel while sentinel runs, expect ended event
12. Long duration survival — 2+ hours idle, agent context < 30K
13. DB resilience — kill/restart sentinel mid-check, no corruption
14. Backward compat — old wait.py alongside sentinel, no interference

### Cadence rule enforcement gap
**Severity:** Medium | **Since:** v4.5 live test (2026-04-03)

Partially addressed by sentinel cadence detection (v5). The sentinel nags when cadence silence exceeds threshold. But the root cause — model-level attention drift during absorptive tasks — remains. The sentinel is the mechanical backstop, not the cure.

See `reviews/v45-live-test/TODO-cadence-escape.md`.

### Remove deprecated scripts
**Severity:** Low | **Target:** v6

`roam_hive_mind_wait.py` and `roam_hive_mind_watchdog.py` are deprecated. Sentinel subsumes both. Remove after confirming no sessions reference them directly.

### Conversation export quality
**Severity:** Low | **Since:** v4

The markdown export (`roam_hive_mind_end`) is functional but minimal. Task state changes aren't timestamped in the export. Lock acquisitions/releases aren't included.

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
