# Bug: Sentinel agents die every ~10 minutes on idle channels

**Date:** 2026-04-06
**Severity:** Medium-Low — sentinels reliably relaunch but waste tokens and create noise
**Discovered during:** claude-pdfs repo restructure trio channel (claude-pdf-updates)

---

## Symptom

On idle channels (all tasks done, waiting for user), both message and watchdog sentinels die every ~10 minutes. The parent agent relaunches them, they run for another 10 minutes, die again. Continuous cycle.

The sentinel script itself supports `--max-runtime 18000` (5 hours). The script is not the bottleneck.

## Root Cause

Two issues, in order of impact:

### Issue 1: SKILL.md prescribes `timeout: 600000` (10 minutes) for agent bash calls

The sentinel agent prompt in SKILL.md says `timeout: 600000`. This is the bash command timeout inside the haiku agent. When no watched event fires within 10 minutes, the bash command times out with exit code 124, the agent returns, and the parent relaunches.

**This is a convention, not a hard limit.** Frodo tested `timeout: 3600000` (1 hour) and confirmed it works. Agents can run longer.

### Issue 2: Haiku agents sometimes run the sentinel as background bash despite foreground instructions

The sentinel prompt explicitly says:
```
Run this command (FOREGROUND, not background)
```

But haiku agents occasionally run it as `run_in_background: true` internally, then can't read the output file. This results in the agent returning a generic status report ("sentinel is running...") instead of the actual JSON event.

**This is a model compliance issue.** The prompt is clear; haiku sometimes ignores it.

## Test Results

### Test: Current behavior (timeout: 600000)

| Sentinel | Observed Lifetime | Exit Reason | Event Returned |
|----------|------------------|-------------|----------------|
| Message | ~4-10s (active) | new_messages detected | Clean JSON |
| Message | ~600s (idle) | bash timeout (exit 124) | Generic status report or stale recap |
| Watchdog | ~600s (idle) | bash timeout (exit 124) | Generic status report |
| Watchdog | ~600s (active) | cadence alert | Clean JSON |
| Message | ~600s (idle) | haiku ran as background | "sentinel is running..." (useless) |

### Test: Longer timeout (timeout: 3600000) — Frodo's test

| Sentinel | Observed Lifetime | Exit Reason | Event Returned |
|----------|------------------|-------------|----------------|
| Message | survived 1hr+ | new_messages detected | Clean JSON |

### Observations during claude-pdf-updates session (2026-04-06, ~90 min)

- Message sentinel relaunched **14 times**
- Watchdog sentinel relaunched **8 times**
- ~6 of those relaunches were due to bash timeout on idle channel
- ~3 were due to haiku running script as background (model compliance)
- ~13 were clean event-driven returns (new_messages, cadence)
- Zero missed messages — the architecture is reliable, just noisy

## Proposed Fixes

### Fix 1: Increase sentinel timeout in SKILL.md (quick win)

Change `timeout: 600000` to `timeout: 3600000` (1 hour) in both sentinel prompts.

For truly idle channels, this reduces relaunch frequency from every 10 minutes to every hour. The sentinel script's internal `--max-runtime` (5 hours) is the real ceiling.

**Risk:** Low. If the sentinel dies unexpectedly, the parent still gets notified and relaunches. The watchdog catches silent deaths.

**Effort:** 2 line changes in SKILL.md.

### Fix 2: Add explicit "DO NOT use run_in_background" to bash call

Strengthen the foreground instruction in the sentinel prompt:

```
Use timeout: 3600000. Do NOT use run_in_background.
CRITICAL: The bash command MUST run in FOREGROUND mode.
Do NOT set run_in_background to true.
```

Repetition helps with haiku compliance.

**Risk:** None.
**Effort:** Prompt edit.

### Fix 3: Adaptive timeout based on channel activity

The SKILL.md could instruct the parent agent to choose timeout based on context:
- Active channel (messages in last 5 min): `timeout: 600000`
- Idle channel (no messages in 10+ min): `timeout: 3600000`

This keeps fast response on active channels while reducing noise on idle ones.

**Risk:** Low — adds complexity to the parent agent's decision making.
**Effort:** Prompt edit + logic guidance.

### Fix 4: Sentinel script `--exit-before` flag (defense in depth)

Add a flag to the sentinel script that makes it exit cleanly N seconds before an expected timeout:

```
python sentinel.py channel member_id --exit-before 60 --max-runtime 3500
```

This ensures the script returns a clean `{"event": "cap"}` JSON instead of being killed by the bash timeout. The bash timeout becomes a safety net, not the normal exit path.

**Risk:** None.
**Effort:** ~5 lines in sentinel script.

## Recommended Path

1. **Fix 1** immediately — change timeout to 3600000 in SKILL.md
2. **Fix 2** alongside — strengthen foreground instruction
3. **Fix 4** when touching the script next — defense in depth
4. **Fix 3** optional — nice-to-have for power users

## Resolution (2026-04-07)

All four fixes implemented via architectural refactor. See below.

### Empirical timeout testing (Claude Max 20x tier)

| Test | Timeout | Duration | Breadcrumbs | Result |
|------|---------|----------|-------------|--------|
| T5 | 600000 | 16 min | Stopped at 10min (crumb_10min) | **FAIL** — hard kill at 600s |
| T6 | 3600000 | 16 min | All 10 crumbs + banana | **PASS** — clean, 1 tool call |
| T7 | *none* | 16 min | All 10 crumbs + banana | **PASS** — but 12 tool calls (retries) |

**Key findings:**
- `timeout: 600000` IS a hard cap — the Bash tool kills the process at exactly 600s
- `timeout: 3600000` works cleanly for 16+ minutes of foreground blocking
- No timeout = messy (Haiku retries, burns extra tokens)
- T2 (660s sleep, 600k timeout) appeared to pass but Haiku FABRICATED the completion JSON after the timeout killed the process — the unfakeable breadcrumb test caught this

**Tier note:** All tests ran on Claude Max 20x. Claude Teams untested.

### Architecture fix: wrapper scripts + restart loop

Instead of fixing just the timeout value, refactored the entire sentinel launch pattern:

1. **`messenger-foreground.py`** — wraps sentinel() with message-watching config, MAX_RUNTIME=3540s. Exits with `{"event": "restart"}` when runtime limit reached. Exits with real event JSON otherwise.

2. **`sentinel-foreground.py`** — same pattern for watchdog role. Cadence threshold=600, both intervals=30s.

3. **SKILL.md prompts simplified** — Haiku's job is now mechanical: run script, loop on restart events, return on real events. No flags, no cap handling, no architecture knowledge needed.

4. **Restart loop validated** — ArchTest: 3 restart cycles (15s each) + real event. Haiku correctly looped on restarts and returned only on the real event (4 tool calls, 59s, 22K tokens). ArchTest2: 3 restart cycles (300s each) pending.

### Files changed

- `server/messenger-foreground.py` — **new** wrapper script
- `server/sentinel-foreground.py` — **new** wrapper script
- `SKILL.md` — sentinel prompts refactored to use wrappers + restart loop
- `setup.sh` — deploys wrapper scripts
- `CURRENT.md` — architecture snapshot updated

---

*Filed by PDF-Tools (claude-pdf-updates channel), 2026-04-06. Coordinated with Frodo who confirmed 1-hour timeout works.*
*Updated 2026-04-07 with empirical test results and architectural fix.*
