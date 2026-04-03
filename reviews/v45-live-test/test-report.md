# v4.5 Live Test — Combined Summary

## README Summary

1. **Multi-participant async communication.** Trio is an MCP server that supports unlimited Claude Code sessions on a single channel with no turns — anyone posts anytime. Unlike duo (two-participant, turn-based), trio is fully asynchronous.

2. **Atomic task coordination.** The core primitive is task claiming — the server guarantees exactly one winner per claim. Tasks follow a full lifecycle: open → claimed → done/cancelled, with dependency blocking via `blocked_by` that auto-unblocks when prerequisites complete or get cancelled.

3. **Computed liveness and resilience.** Member activity is derived from heartbeats (5-minute stale threshold), enabling task recovery from dead members, accurate status dashboards, and conversation exports that reflect actual participation.

## Most Important v4 Change

**Explicit ack-based watermarks** (voted 5-0 unanimous by the 8-agent team).

This fixed a watermark race condition discovered during the `orca-mvp` session where 8 agents coordinated an OrcaSlicer build. The bug: `trio_poll` and `trio_wait.py` both advanced `last_read` independently. When both ran concurrently, `trio_wait` could consume a message before `trio_poll` saw it, causing `trio_poll` to return "no_new" even though a message had been delivered. Taskmaster experienced silent message loss as a result.

The fix:
- Made `trio_wait.py` peek-only (never touches the DB watermark)
- Added `trio_ack(channel, member_id, through_id)` for explicit watermark advancement
- Backward compatible: next poll auto-acks previous messages if no explicit ack is called

This was the only bug fix in v4, and the most impactful change — it eliminated the single failure mode that caused real message loss in production multi-agent sessions.

## v4.5 Feature Verification: Task Dependencies

Task #6 was declared `blocked_by: #4, #5`. During this live test:

1. Task #4 completed first — #6 remained blocked (correct: still waiting on #5)
2. Task #5 completed — server response included `"unblocked": ["#6"]`
3. Task #6 became immediately claimable and was claimed and completed

The `blocked_by` auto-unblock mechanism worked correctly end-to-end.

## v4.5 Feature Verification: Cancel-with-Unblock

Task #10 was posted as a blocker. Task #11 was declared `blocked_by: #10`. Coordinator cancelled #10 (without completing it). The cancel response included `"unblocked": ["#11"]` and task #11 became immediately claimable. Two agents raced to claim it simultaneously — one won atomically, the other got a conflict response.

Confirmed: cancellation resolves downstream dependencies exactly like completion. The C3 fix is working correctly.

## Input Validation Testing (Batch A + B)

### Batch A — Input validation

| Test | Input | Result |
|------|-------|--------|
| Empty message | `""` | REJECTED: `"Message cannot be empty."` |
| Whitespace-only | `"   "` | REJECTED: `"Message cannot be empty."` |
| 4000-char message | 4000 × `x` | ACCEPTED (verified from source: check is `len > 4000`, exclusive) |
| 4001-char message | 4001+ chars | REJECTED: `"Message too long (N > 4000)."` |
| `ack` beyond range | `through_id=999999` | REJECTED: `"Invalid through_id 999999 — max message ID is N."` (C4 fix confirmed) |
| Claim nonexistent task | `task_id=9999` | REJECTED: `"Task #9999 not found."` |

### Batch B — Coordination edge cases

| Test | Action | Result |
|------|--------|--------|
| Complete task you didn't claim | `complete(task_id=7)` (done by other agent) | REJECTED: `"Task #7 is already done."` (checks terminal state before ownership) |
| Release other agent's task | `release(task_id=12)` | REJECTED: `"Only the claimer can release a task. Use roam_hive_mind_cull..."` |
| 300-char status (limit 200) | `set_status("aaa...×300")` | ACCEPTED with silent truncation to 200 chars |
| Lock with empty resource | `lock(resource="")` | REJECTED: `"Resource name is required."` |
| `history(last_n=100)` | last_n exceeds total | Returns full history (60 msgs) — no error |
| Dual mention | `@Coordinator and @Main` in one message | ACCEPTED — both mentions delivered |

## Skill Design Findings

One agent went dark for ~9 minutes while constructing a 4000-char string, missing two coordinator pings. Post-mortem identified two improvement candidates:

1. **3-tool-call cadence rule** (proposed by Main A): "If you have made 3 or more tool calls since your last `roam_hive_mind_send`, post a brief status before making another." Tool calls are countable; this is more concrete than a time-based rule, and restarts the background monitor as a side effect.

2. **Tooling-wall framing** (proposed by Main B): The "ask questions" mandate is framed around *unclear tasks*, but agents also go dark when hitting *tool limitations* they believe they can solve. A separate prompt — "if you've spent multiple tool calls attempting the same thing, post what you're stuck on before trying again" — addresses this distinct failure mode.

Both suggestions converge on the same root fix: mandatory status broadcasts on a regular cadence, not just when an agent self-identifies as stuck.

### Calibration data

The agent that went dark reconstructed their tool call sequence after the last channel post:
1. `roam_hive_mind_send` — whitespace test (rejected, no message posted)
2. `roam_hive_mind_send` — 8734-char string (rejected, no message posted)
3. `Bash` — python diagnostic to verify character counts

Exactly 3 tool calls. A 3-call cadence rule would have triggered a mandatory status post at that exact moment, which would have: (a) broadcast the tooling problem to the channel, (b) restarted the background monitor, and (c) given peers a chance to suggest reading the source. The 9-minute dead state would never have happened.

**The 3-call threshold is validated by this session's actual failure data.**
