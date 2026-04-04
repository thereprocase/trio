# TODO: Cadence Rule Violation — v4.5 Live Test (Frodo, task #23)

**Date Discovered:** 2026-04-03
**Channel:** `skill-cleanup`
**Agent:** Frodo
**Task:** #23 (README review)
**Severity:** Medium (behavioral rule, not functional bug)

## What Happened

Frodo violated the 3-call cadence rule during task #23 (README review). The agent made **11 work tool calls** (globs, greps, reads) across multiple files without posting a single status update. The cadence rule requires a status post with confidence every 3 work calls. Frodo owed **3 checkpoints** and posted **zero**.

## Context

- Frodo loaded the skill via `/trio`, so `SKILL.md` with the cadence rule was in context
- v4.8 server footers (which remind on every message) were active
- Despite both reinforcement mechanisms, the agent's "deliver a complete picture" instinct overrode the cadence rule

## Root Cause (Agent Self-Report)

> "I got absorbed in the audit. The findings were stacking up and I wanted to deliver a complete picture rather than fragments. Classic 'I'll just check one more thing' loop."

## Analysis

Same failure mode as the original 9-minute gap (Batch A agent), except:

- **Batch A agent** had only `SKILL.md` rules (v4.5, no cadence rule yet)
- **Frodo agent** had:
  - Cadence rule in `SKILL.md` (v4.8)
  - Server-side footer reminders (every message)
  - Had just participated in a brainstorm about why the cadence rule matters

If the rule exists, is in context, has server-side reinforcement, and the agent STILL ignores it, the question is: what additional mechanism would make it stick?

## Open Questions

1. Could the server count work tool calls and refuse to execute them until a status is posted? (Requires server-side state per member)
2. Could a hook count tool calls and inject a reminder into the agent's context?
3. Is this fundamentally a model-level attention issue that no amount of prompting can fix?
4. Should the cadence threshold be lower (2 calls instead of 3) for absorptive tasks like audits?

**Tags:** trio-dev, behavioral-rules, cadence-violation
