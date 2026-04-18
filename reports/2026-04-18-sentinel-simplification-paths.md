# Sentinel Simplification — Paths Forward Without Anthropic Changes

**Date:** 2026-04-18
**Context:** v6.2 shipped (capability-scoped subagent, session tokens). Over-reach bug structurally fixed. Under-reach bug (Haiku refuses the blocking 1h contract) remains. The durable answer is a harness-provided non-reasoning blocking-tool primitive — that requires Anthropic to ship a feature. This doc captures what we can do without waiting.

## Problem recap

The sentinel is an economic adapter: Opus parent stays cheaply "asleep" by proxying the wait through a Haiku sub-agent's tool call. Alternative: parent polls at Opus rates (too expensive). The Haiku sub-agent is the only way today to absorb multi-hour waits without burning parent tokens.

Failures in the current design:
1. **Over-reach** — Haiku posts to the channel. Fixed in v6.2 via `trio-sentinel` subagent_type with `tools: Bash` only.
2. **Under-reach** — Haiku refuses to actually run the 1h blocking Bash call. No prompt discipline fixes this reliably. Not in scope for v6.2.

## The ideal fix (blocked on Anthropic)

A harness-provided `Subprocess(cmd, timeout, block=true)` tool that:
- Runs a shell command
- Suspends the agent process entirely — zero inference tokens during wait
- Returns stdout when the subprocess exits or timeout fires
- No sub-agent, no model, no contract to hold

Close cousin of Claude Code's existing `run_in_background=true` Bash call — the gap is a "wake me when the background Bash finishes" wait-pattern that doesn't force the parent to poll `BashOutput`.

**Action:** file an RFC with Anthropic. Don't block on it.

## Practical paths today (no Anthropic change needed)

### Path 1 — Shrink the sentinel contract

Drop `MAX_RUNTIME_S` from 59 min to 10 min (or 5). Haiku has less rope to drift, so fewer silent bails.

- **Cost:** 6× more restart cycles per hour. Each restart is one Opus inference. Rough estimate: +$1-2/hour idle overhead.
- **Reliability:** Up — each contract is short enough that Haiku rarely gets bored and bails.
- **Complexity:** One constant change. No architectural shift.
- **When to pick:** Default first move. Cheap to try, trivial to revert.

### Path 2 — OS daemon + peek-only

Launch a native `pythonw` daemon at `nth_connect`. Daemon writes events to SQLite (already how the server works) + optionally a named pipe. Kill both Haiku sentinels entirely. Parent relies on peek polls at natural breakpoints.

- **Cost:** None during active work (peek polls are already in the skill).
- **Reliability:** High for the push part (daemon doesn't drift). Low for idle periods — if the user goes AFK and a peer posts, the parent sits silent until the user comes back or the parent makes a tool call that triggers a peek.
- **Tradeoff:** Loses push-during-idle semantics. For pair-coding, fine. For long-running async collab (RAMBurn/RAMWatch sprints), regression.
- **Complexity:** New daemon lifecycle (PID file, reap on `nth_end`, handle orphans). Modest.
- **When to pick:** If under-reach is causing more pain than idle-blindness.

### Path 3 — File-flag pipe via Bash (recommended experiment)

OS daemon writes an event flag to a named pipe or signal file. Parent calls `Bash(timeout=600000)` directly with something like `read -t 590 line < /tmp/nth-events-<channel>.pipe; echo "$line"`. The Bash tool blocks on the pipe read without any sub-agent involvement.

- **Cost:** Capped at 10-min blocks by Bash tool ceiling. ~6 Opus wake-ups/hour when idle. Each wake is one inference plus a re-invoked Bash call. Still cheaper than polling every 30s.
- **Reliability:** Full. No Haiku in the loop — Bash is a deterministic harness tool that will block for exactly the timeout or until the pipe has data. No drift possible.
- **Capability:** Bug class goes away. The `trio-sentinel` subagent template becomes moot. No capability scoping needed because there's no sub-agent.
- **Complexity:** Need a daemon lifecycle (Path 2's cost). Need to manage named pipes across Windows/WSL/Linux (WSL has `/tmp`, Windows needs a different path scheme — possibly a socket or poll file).
- **When to pick:** Default recommendation. If it holds in one channel's worth of soak testing, it's a real simplification.

## Recommendation

1. **Try Path 3 as an experiment on one channel.** Modest daemon + one-line Bash blocker. Soak test for 4+ hours. If no silent drops, consider rolling it into v7.
2. **If Path 3 is worse than expected**, fall back to Path 1 (shrink sentinel contract). Trivial revert.
3. **File the Anthropic RFC either way.** The durable answer is still upstream. The paths above buy reliability but don't eliminate the cost overhead vs. the hypothetical primitive.

## What would go away if Path 3 ships

- Both sentinel sub-agents (messenger + watchdog)
- `~/.claude/agents/trio-sentinel.md`
- `messenger-foreground.py` and `sentinel-foreground.py` Python wrapper scripts (logic moves into the daemon)
- Most of `nth_sentinel.py`'s adaptive-mode logic (active/idle/sleep transitions exist to self-throttle Haiku tokens; daemon has no tokens to throttle)
- SKILL.md "MANDATORY: Background Monitoring (v5 Sentinel)" section (~80 lines)
- Peek polls section (~15 lines; subsumed)
- "Expect long silence" + "If sentinel launch fails" prose
- The whole over-reach bug class

What stays:
- Cadence rules, stay-connected, ask-questions (agent behavior, not sentinel mechanics)
- 18 MCP tool surface (task coordination, locks, mentions, retraction)
- Session token machinery (v6.2) — still useful for provenance and watermark isolation even without sub-agents
- Status/dashboard rendering
- Channel end + export

Realistic SKILL.md reduction: ~100-130 lines, plus the whole capability-scoping apparatus the v6.2 patch just built. ~20% of the file. Reliability win is disproportionately large vs. byte count — the two pieces that fail in practice collapse into one harness call with no model in the loop.

## Open questions

- **Windows/WSL pipe compatibility.** Named pipes work differently on Windows (`\\.\pipe\name` vs. Unix `/tmp/fifo`). Path 3 daemon needs to handle both. Possible fallback: use a poll file with `inotifywait` (Linux) / `ReadDirectoryChangesW` (Windows) / file mtime poll.
- **Daemon reaping on unexpected parent death.** If Claude Code crashes, the daemon orphans. PID file + startup-time stale-PID check is standard, but needs to be bulletproof.
- **Quartet (SSE/cross-machine) implications.** Path 3 assumes the daemon is local. For quartet, each machine runs its own daemon; the MCP/DB coordination handles cross-machine sync. Should work, but needs verification.
- **Cost comparison vs. Haiku sub-agent.** Haiku 59-min block is ~cheap. Path 3 replaces one Haiku hour with ~6 Opus wakes. Need real numbers on a typical channel's activity profile to decide if the reliability is worth the cost at scale.

## References

- Original bug: `bugs/2026-04-17-sentinel-agent-tool-scope.md`
- v6.2 design council: `reviews/2026-04-17-v6.2-council-brainstorm.md`
- Aragorn v6.2 review: `reviews/2026-04-17-v6.2-aragorn-security-review.md`
- v6.2 release: `reports/2026-04-17-v6.2-release.md`
