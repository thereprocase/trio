# TODO — Trio

## Open

### Cadence rule enforcement gap
**Severity:** Medium | **Since:** v4.5 live test (2026-04-03)

The 3-call cadence rule is violated under "absorptive" tasks (audits, deep reads) even with server-side footer reminders and SKILL.md instructions. Frodo violated it with 11 consecutive work calls during a README review (see `reviews/v45-live-test/TODO-cadence-escape.md`).

**Open questions:**
- Server-side call counting with enforcement (refuse tool execution until status posted)?
- Hook-based reminder injection?
- Lower threshold (2 calls) for absorptive task types?
- Fundamental model-level attention limit that prompting can't fix?

### Agent-monitor live validation
**Severity:** Low | **Since:** v4.9 (2026-04-06)

The agent-based idle monitor was validated with synthetic tests (20-loop durability, background notification). Needs validation in a real multi-agent session with actual idle periods. Key things to observe:
- Does the 30-cycle cap fire correctly?
- Does the parent re-launch cleanly after cap or message detection?
- Does the permission gate naturally satisfy from prior Bash usage?
- What's the actual context growth per cycle in production?

### Conversation export quality
**Severity:** Low | **Since:** v4

The markdown export (`roam_hive_mind_end`) is functional but minimal. Task state changes aren't timestamped in the export. Lock acquisitions/releases aren't included. Member join/leave events are embedded in the message stream rather than called out separately.

## Completed (v4.9)

- [x] Agent-based idle monitoring (95% token reduction)
- [x] Fix misleading watermark comment in `roam_hive_mind_poll`
- [x] Gitignore `settings.local.json` and `.env` files
- [x] Add `CLAUDE.md` project guide
