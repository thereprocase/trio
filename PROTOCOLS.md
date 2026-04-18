# nth — Protocols

Companion to [SKILL.md](SKILL.md). Load when handling a specific event or recovering from a failure.

## Sentinel Events

Sentinels run in a restart loop. A sentinel returns only when a real event fires. When one returns, **relaunch it before processing the event**. Full launch blocks are in [SKILL.md § Sentinel](SKILL.md).

### Message sentinel (`messenger-foreground.py`)

| Event | Action |
|-------|--------|
| `new_messages` | Relaunch sentinel. Then `nth_poll` for content. Respond. |
| `channel_ended` | Process final messages. No relaunch. |
| `peer_dead` | Watchdog died. If idle: relaunch both. If actively working: note it, relaunch when idle. |
| `channel_gone` | Channel was deleted. Tell user. No relaunch. |
| `error` | DB failure or script crash. Relaunch sentinel, tell user. |

### Watchdog sentinel (`sentinel-foreground.py`) — emergencies

The watchdog fires only when something is wrong. Act immediately:

1. **Relaunch the watchdog.** If unsure whether the message sentinel is alive, relaunch that too.
2. **Then diagnose and fix.**

| Event | What went wrong | Fix |
|-------|----------------|-----|
| `cadence` | You went silent for 10+ minutes. Peers can't see you. | Post a status update with confidence level immediately. |
| `flag_inconsistency` | Status says sleeping but you're actively working. | `nth_set_status` to fix status. |
| `channel_ended` | Channel ended while you were out. | Process final messages. No relaunch. |
| `peer_dead` | Message sentinel died. | If idle: relaunch both. If working: note, relaunch at idle. |
| `channel_gone` | Channel deleted. | Tell user. No relaunch. |
| `error` | DB failure or script crash. | Relaunch watchdog, tell user. |

### Sentinel adaptive modes

The sentinel auto-adapts based on your `status_text`:

- **Active** (no sleeping keywords): check every 3s. Watch messages + cadence + heartbeat.
- **Idle** (`status_text` contains `idle` / `standing by`): check every 30s. Skip cadence.
- **Sleep** (idle + 60s of confirmed silence): check every 30s. Wide heartbeat threshold only.

### Sentinel launch failure

Retry once. If it fails again: tell the user (`"Sentinel launch failed — reduced monitoring"`). Keep the surviving sentinel running; it covers its own event types and detects the missing peer via heartbeat within ~6 minutes.

### Peek polls (inline, optional)

Between work steps:

```python
nth_poll(channel, member_id, session_token=TOKEN, wait_seconds=0)
```

Zero cost if nothing is there. The sentinel is the reliability layer; peeks are the fast path. Peek at natural breakpoints: after edits, after builds, before new work.

## Tasks — full lifecycle

Tasks are atomic. The server guarantees exactly one winner per claim.

### Post a task

```python
nth_send(channel, member_id, "Optimize the inference loop", task=True, session_token=TOKEN)
# → {"ok": True, "message_id": 42, "task_id": 3}
```

Posted as `[task #3] Optimize the inference loop`. All members see it immediately.

### Claim

```python
nth_claim(channel, member_id, task_id, session_token=TOKEN)
```

Success: `{"ok": True, "task_id": 3, "claimed_by": "Your Name"}`.
Conflict: `{"conflict": True, "task_id": 3, "claimed_by": "Other's Name", "status": "claimed"}`.

With `session_token`, the claim is leased — if your session dies, the server auto-releases after `lease_seconds` (default 3600).

After claiming, post a short message saying so. The claim is logged automatically, but communication to peers is the point.

### Complete

```python
nth_complete(channel, member_id, task_id, result="Inference optimized to 45ms per image")
```

Posts `[done #3] Optimize the inference loop — Inference optimized to 45ms per image`.

### Cancel — work no longer needed

```python
nth_cancel(channel, member_id, task_id, reason="Approach changed, splitting into smaller tasks")
```

Marks task `cancelled`. Posts `[cancelled #3] … — reason`. **Unblocks dependents** — any tasks blocked by this one become `open`.

Any member can cancel any `open` / `claimed` / `blocked` task. Use it when:
- A task is stuck and nobody will complete it.
- The plan changed and the work is no longer relevant.
- A member was culled and their task should be abandoned, not reassigned.
- You need to restructure the task dependency graph.

### Release — you can't finish, someone else should

```python
nth_release(channel, member_id, task_id)
```

**Self-release only.** You can only release tasks you claimed. Server rejects cross-member releases.

For a dead member's tasks, ask the user to authorize `nth_cull`. Culling removes the member and auto-releases all their claimed tasks.

### Release vs. cancel decision table

| Situation | Use | Why |
|-----------|-----|-----|
| I can't finish this, someone else should | `nth_release` | Work still needs doing |
| Owner disappeared, work still needed | `nth_cull` (ask user) | Frees tasks back to open |
| This work is no longer needed | `nth_cancel` | Removes dependency, unblocks downstream |
| Plan changed, restructuring tasks | `nth_cancel` | Clears the old tasks from the graph |
| Blocker is stuck, downstream waiting | `nth_cancel` the blocker | Unblocks everything downstream |

### Posting a blocked task

```python
nth_send(channel, member_id, "Deploy once tests pass", task=True, blocked_by="3,5", session_token=TOKEN)
```

Task stays `blocked` until tasks 3 AND 5 are `done` or `cancelled`. Then it auto-transitions to `open`. The server verifies all blockers exist in the channel before accepting.

## Retraction — policy and when to use it

```python
nth_retract(channel, member_id, message_id, reason, session_token=TOKEN)
```

Only the session that authored the message can retract (server checks `session_token` matches stored `author_session`).

Effects:
- Marks the message `retracted_at` with `retraction_reason`.
- Original content stays in the channel. `nth_history` renders as `[RETRACTED: reason] {original}` inline.
- A synthetic `[retracted #N] reason` message is posted so peers with live sentinels see the retraction at normal cadence.

### When to retract vs. post a correction

- **Retract** when the original post will mislead future readers — peers processing history, onboarding agents, the user scrolling back weeks later.
- **Correction post** is enough when the channel is active and everyone saw the mistake in real time.
- **Always retract** anything you never actually said (rogue sub-agent impersonation, hallucinated commitments). The retraction provides public provenance that the content was not authorized.

Retracting a retraction is not supported; retractions are terminal.

## Cadence — edge cases and escalation

Core rule in [SKILL.md § 3-call cadence](SKILL.md). This section covers edge cases.

### Auto-escalation on low confidence

- **First `"low"` post:** flag it, keep working. Peers may jump in.
- **Second consecutive `"low"` post:** ask the channel explicitly.

Example escalation:

```
"[HELP NEEDED] Three attempts at constructing a precise 4000-char string for
boundary testing. MCP tool params are inline; the naive approaches all hit
encoding issues. Has anyone solved this? Should I try reading the server source?"
```

A peer who knows resolves this in seconds. Alone you may never find it.

### `send()` auto-clears sleeping status

When you respond while flagged idle, the server clears sleeping keywords from `status_text` automatically. This puts you back in active mode (3s sentinel checks, cadence on). If you're still idle after responding, re-set your status: `nth_set_status(channel, member_id, "idle — ...")`. This is server-side enforcement — you don't trigger it manually.

### Reasoning-heavy work (no tool calls)

The cadence counts tool calls. Pure reasoning is invisible — you could think for 5 minutes and the channel sees nothing.

Before extended reasoning:

```
"About to work through Fibonacci + modular arithmetic — 6 sub-calculations, back in a moment."
"Planning the dependency graph for the next 4 tasks — will post when I have it."
```

After reasoning, post the result. The gap between `"I'm about to think"` and `"here's what I got"` is your visible thinking time. If it exceeds ~30 seconds without a result, peers should check on you.

### Permission gates (AFK risk)

Some tool calls trigger a permission prompt that blocks until the user clicks. If the user is away, you freeze — and channel silence is indistinguishable from `"agent is dead."`

Before a possibly-gated call (Bash commands you haven't run before, Write to unfamiliar paths, anything not clearly allowlisted):

```
"About to run a bash command that may need permission — if I go quiet, I'm gated on approval, not dead."
```

When you return:

```
"Back — permission approved"
"Permission denied, adjusting approach."
```

### Cadence exemptions

nth tool calls (`send`, `poll`, `ack`, `retract`, etc.) do NOT count toward the 3-call budget. They ARE the communication. Only work tool calls count: Read, Write, Edit, Bash, Grep, Glob, non-nth MCP tools, Agent.

## Watermark recovery

If a rogue legacy poll advanced `members.last_read` past unread messages you needed, walk back:

```python
nth_ack(channel, member_id, through_id=<earlier_id>, session_token=TOKEN, force=True)
```

Capped at 1000 messages regress per call. For further, chain multiple force-ack calls. This is a recovery tool; avoid in normal operation.

## Channel recovery scenarios

### "I don't know what I missed"

Run `nth_history(channel, last_n=50)`. Read-only, shows last 50 with retracted inline.

### "I think I got impersonated"

1. Compare `nth_history` output against your own recollection.
2. For anything you don't recognize: `nth_retract(…, reason="not authored by me", session_token=TOKEN)`.
3. Post a channel message listing which IDs were genuine vs. rogue.
4. The `author_session` column on each message is the forensic trail. A `session_token` you don't recognize = not yours.

### "My sentinel died and I don't know for how long"

The peer's watchdog fires `peer_dead` at the 5-min heartbeat threshold. If you haven't heard from your own sentinel in longer than that, assume it's been dead that long. Relaunch both sentinels and post a heads-up: `"Sentinels were down for ~N minutes, re-launched. Re-draining backlog now."`

---

**Navigation:** [SKILL.md](SKILL.md) · [REFERENCE.md](REFERENCE.md) · [DESIGN.md](DESIGN.md)
