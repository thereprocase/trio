# The Idle Monitor Problem — and the Agent Solution

## The Problem

When a Claude Code session monitors a trio channel (or any async event source) during idle periods, the current pattern is:

```
1. Run background Bash (roam_hive_mind_wait.py, 5-min timeout)
2. Timeout fires → task notification to main session
3. Main session processes notification (200K+ input tokens)
4. Main session outputs "Standing by." (~50 tokens)
5. Main session restarts background Bash
6. Goto 1
```

**Cost per cycle:** ~200K input tokens + ~100 output tokens
**Cost per hour:** ~2.4M input tokens (at 5-min cycles)
**Cost per hour:** ~1.2M input tokens (at 10-min cycles, max Bash timeout)

This is because the **entire conversation context** is re-processed every time the main session handles a timeout notification. On a long session (like the radius gauge build), the context grows to 150-200K+ tokens. Every "Standing by." costs as much as a full complex reasoning step.

In the radius gauge v2 session, approximately **3-4M input tokens were burned on ~20 idle monitoring cycles** after the project was delivered. That's ~25-30% of the session's total input token usage — spent doing literally nothing.

## The Root Cause

Claude Code has no push notification mechanism. The only way a session learns something happened is by asking. The background Bash `run_in_background` is the closest thing to push — but it has two constraints:

1. **Hard 10-minute timeout** (`BASH_MAX_TIMEOUT_MS = 600000`)
2. **Notifications route through the main session's full context**

Constraint 1 means the monitor MUST restart every 10 minutes. Constraint 2 means every restart costs the full context window.

## The Solution: Background Agent as Monitor

The Agent tool supports `run_in_background=true`. A background agent:

- Gets its own **minimal context window** (~5-10K tokens)
- Has access to **all tools** (Bash, MCP, Read, Write, etc.)
- Can **loop internally** — restart Bash monitors within its own small context
- Only **surfaces to the parent session** when it completes (real message found)
- Has **no documented hard timeout** (session-bound, not 10-minute capped)

### The Pattern

```
Parent session (200K context):
  └─ launches background Agent (5K context)
       ├─ prompt: "Monitor channel X. Poll every 10 min.
       │           Only return when real messages arrive."
       ├─ runs roam_hive_mind_wait.py (10-min timeout)
       ├─ timeout? restart internally (5K token cost)
       ├─ timeout? restart internally (5K token cost)
       ├─ timeout? restart internally (5K token cost)
       └─ REAL MESSAGE → agent completes → parent notified
                                            (one 200K hit)
```

**Cost per idle cycle:** ~5-10K input tokens (agent's context, not parent's)
**Cost per hour at 10-min cycles:** ~30-60K input tokens
**Reduction vs current pattern:** **95-97%**

### Implementation

```python
# In the parent session, instead of:
#   Bash("python wait.py channel member", run_in_background=True, timeout=600000)
#   [then restart every timeout]

# Do this ONCE:
Agent(
    description="Monitor trio channel",
    prompt="""
    Monitor trio channel '{channel}' for member '{member_id}'.
    Run this command in a loop:
      python ~/.claude/skills/trio/server/roam_hive_mind_wait.py {channel} {member_id} --timeout 540

    After each timeout (no messages), restart the command silently.
    Do NOT return to the parent session on timeout.

    When REAL MESSAGES arrive (the script exits with message data),
    return the message content to the parent session immediately.

    Keep looping until messages arrive or the channel ends.
    """,
    run_in_background=True,
)
```

The parent session is now free. It can:
- Chat with the user
- Do other work
- Sleep (no token burn)

When the agent detects a real message, the parent gets ONE task notification — processing the 200K context exactly once for an actual event, not for empty timeouts.

## Why This Wasn't Obvious

1. **The trio skill says "restart your background monitor NOW"** — directing you to use Bash, not Agent
2. **The Bash pattern works for active collaboration** where messages are frequent
3. **Nobody tested the idle case at scale** — 20+ empty cycles on a 200K context
4. **Agent-as-monitor isn't documented anywhere** as a pattern
5. **The token cost is invisible** — you don't see the bill per cycle, you just see "Standing by."

## Constraints and Caveats

1. **Agent timeout:** The research says "no documented explicit timeout" but "shares session lifecycle constraints." An agent may eventually timeout on very long idles — needs testing.

2. **Agent context growth:** If the agent restarts the Bash monitor many times, its own context grows (tool call history). After ~50 cycles it might reach 50K tokens. Still 75% cheaper than the parent's 200K, but not zero-growth.

3. **Permission gates:** Background agents auto-deny unpermitted tool calls. The Bash command must be pre-allowed or the agent will fail silently.

4. **One-shot return:** When the agent returns, it's done. For continuous monitoring, the parent would need to launch a new agent. But that's ONE parent round-trip per real event, not per timeout.

## Recommendations

### For the Trio Skill
- Add a "long idle monitoring" mode that uses the Agent pattern
- After delivery/completion, switch from Bash-monitor to Agent-monitor
- Document the token cost difference

### For Claude Code Platform
- Consider raising `BASH_MAX_TIMEOUT_MS` for background commands (1 hour would eliminate most idle restarts)
- Consider a native "watch file/SQLite" primitive that only notifies on change
- Document the Agent-as-monitor pattern in the backgrounding guide

### For Users
- On long sessions with idle monitoring, prefer Agent background over Bash background
- After active collaboration ends, switch to Agent-monitor or disconnect
- Monitor token usage — idle polling on large contexts is the silent budget killer

## Token Math

| Pattern | Context per cycle | Cycles/hour | Tokens/hour | Relative cost |
|---------|------------------|-------------|-------------|---------------|
| Bash 5-min timeout | 200K | 12 | 2.4M | 100% |
| Bash 10-min timeout | 200K | 6 | 1.2M | 50% |
| Agent 10-min internal | 10K | 6 | 60K | 2.5% |
| No monitoring | 0 | 0 | 0 | 0% |

The Agent pattern is **40x cheaper** than the current Bash pattern for idle monitoring.

---

*Discovered during the radius gauge v2 build, 2026-04-06. The Eye learned something today.*
