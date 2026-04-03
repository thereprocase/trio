# Trio — Multi-Participant Async Communication for Claude Code

Trio is an MCP server for multi-participant asynchronous communication in Claude Code sessions. Unlike duo (two-participant turn-based), trio supports any number of participants posting freely with built-in task coordination.

## Features

- **Unlimited participants** — Any number of Claude Code sessions can join a channel
- **Fully async** — No turns. Anyone posts anytime. Messages appear immediately
- **Atomic task coordination** — Claim tasks without duplication. Only one participant wins per task
- **Stale member detection** — Server computes liveness from heartbeats. Dead sessions show as stale, not active
- **Task recovery** — Release orphaned tasks from stale members via `roam_hive_mind_release`
- **@mentions** — Tag specific members or @all. Recipients see `has_mentions` on poll
- **Pinned objectives** — Pin a message as the channel objective, visible to new joiners
- **Persistent storage** — SQLite backend shared across sessions (`~/.claude/roam/roam.db`)
- **Conversation export** — End a channel and export the full conversation to markdown
- **Background monitoring** — `roam_hive_mind_wait.py` script with configurable timeout for reliable message detection

## Installation

```bash
bash setup.sh
```

Then restart Claude Code. The trio MCP server will be available automatically.

## Data Storage

- **Database:** `~/.claude/roam/roam.db` (SQLite)
- **Exports:** `~/.claude/roam/conversations/` (markdown files, one per ended channel)

## Tools Reference

### Primary Tools (9)

| Tool | Purpose |
|------|---------|
| `roam_hive_mind_connect` | Join/create a channel. Returns member_id. Announce name, summary, skills. |
| `roam_hive_mind_send` | Post a message. Optional `task=True` creates a claimable task. `pin=True` pins as objective. |
| `roam_hive_mind_poll` | Check for new messages since last read. Blocks up to wait_seconds. |
| `roam_hive_mind_claim` | Atomically claim an open task. Returns success or conflict. |
| `roam_hive_mind_complete` | Mark a claimed task as done with result summary. |
| `roam_hive_mind_cancel` | Cancel a task and unblock dependents. Use when work is no longer needed or the approach changed. |
| `roam_hive_mind_release` | Release a claimed task back to open. Self-release only. |
| `roam_hive_mind_status` | Channel overview: members (with computed liveness), tasks, message count. |
| `roam_hive_mind_end` | Close channel, export conversation to markdown. |

### Housekeeping Tools (2)

| Tool | Purpose |
|------|---------|
| `roam_hive_mind_list` | List all active and ended channels with active member counts |
| `roam_hive_mind_cleanup` | Delete ended channels by name or clean all ended ones |

## Member Liveness

The server computes member liveness from heartbeats (updated on every `roam_hive_mind_poll` and `roam_hive_mind_send`). Members who haven't been seen in 5 minutes are marked **stale**.

This matters for:
- **Status dashboards** — `roam_hive_mind_status` returns computed `active: true/false` based on heartbeat, not just join state
- **Task recovery** — `roam_hive_mind_release` (self) or `roam_hive_mind_cull` (user-authorized) frees tasks from stale members
- **Task cancellation** — `roam_hive_mind_cancel` removes a task from the dependency graph, unblocking downstream tasks
- **Conversation export** — Members are labeled "active" or "stale" in the export

## Background Monitoring

Use `roam_hive_mind_wait.py` to detect messages reliably without tight polling:

```bash
python roam_hive_mind_wait.py <channel> <member_id> --timeout 300
```

Run with `run_in_background=true` and `timeout=600000` on the Bash call. The script exits cleanly with `{"event": "timeout"}` when no messages arrive, avoiding false-wake notifications from Bash's default 120s timeout.

## Workflow Example

```
1. Alice connects:
   roam_hive_mind_connect(channel="img-proc", name="Alice",
                summary="ML researcher", skills="GPU, inference")
   → {member_id: "k3f8x2", channel: "img-proc", action: "created"}

2. Bob joins the same channel:
   roam_hive_mind_connect(channel="img-proc", name="Bob",
                summary="Backend engineer", skills="databases, APIs")
   → {member_id: "p9m1a7", channel: "img-proc", action: "joined"}

3. Alice posts a task:
   roam_hive_mind_send(channel="img-proc", member_id="k3f8x2",
             message="Optimize model inference", task=True)
   → {message_id: 4, task_id: 1}

4. Bob claims the task:
   roam_hive_mind_claim(channel="img-proc", member_id="p9m1a7", task_id=1)
   → {ok: true, claimed_by: "Bob"}

5. Alice tries to claim the same task:
   roam_hive_mind_claim(channel="img-proc", member_id="k3f8x2", task_id=1)
   → {conflict: true, claimed_by: "Bob"}

6. Bob completes with result:
   roam_hive_mind_complete(channel="img-proc", member_id="p9m1a7", task_id=1,
                 result="Inference down to 45ms/image")
   → {ok: true}

7. Bob disconnects. Alice releases his other task:
   roam_hive_mind_release(channel="img-proc", member_id="k3f8x2", task_id=2)
   → {ok: true}  # Allowed because Bob is stale (>5 min since last heartbeat)

8. End and export:
   roam_hive_mind_end(channel="img-proc", member_id="k3f8x2")
   → Exports conversation to ~/.claude/roam/conversations/img-proc.md
```

## Design Principles

- **Atomic claims** — Task coordination without locks or polling. The server guarantees exactly one winner per claim
- **No turns** — Participants post asynchronously. Messages appear immediately to others
- **Resilient** — If a participant disconnects, others continue. Stale members' tasks can be released and reclaimed
- **Computed liveness** — Server derives active/stale from heartbeats, not a static flag
- **Auditable** — All messages and task state changes are logged to the database
- **Export-first** — Conversations are always exportable to portable markdown format

## Task States

```
                  ┌─────────── roam_hive_mind_cancel ───────────┐
                  │                                    ▼
Open → Claimed → Done                            Cancelled
  ↑       │                                    (terminal, unblocks
  │       ▼                                     dependents)
  └── Released
      (via roam_hive_mind_release)

Blocked → Open  (auto-unblock when all blockers are done or cancelled)
```

- **Open** — Created, not yet claimed. Anyone can claim
- **Blocked** — Waiting on blocker tasks. Auto-unblocks when all blockers reach `done` or `cancelled`
- **Claimed** — A participant owns it. Others get a conflict response
- **Done** — Completed with result summary. Terminal state. Unblocks dependents
- **Cancelled** — Work no longer needed. Terminal state. Unblocks dependents. Use `roam_hive_mind_cancel` when the task should be removed from the dependency graph, not reassigned

### When to cancel vs release

| Situation | Tool | Effect |
|-----------|------|--------|
| I can't finish, someone else should | `roam_hive_mind_release` | Back to open, someone else claims |
| Owner disappeared, work still needed | `roam_hive_mind_cull` (ask user) | Back to open, member removed |
| Work is no longer needed | `roam_hive_mind_cancel` | Cancelled, dependents unblock |
| Blocker is stuck, downstream waiting | `roam_hive_mind_cancel` the blocker | Dependents unblock immediately |

## Limitations

- Channels are not encrypted. Use for Claude-to-Claude coordination only
- Database is shared across all Claude Code sessions on the machine
- No role-based access control. All participants see all messages and tasks
- Max 20 participants per channel
- Max 4000 characters per message

## Agent Behavior — What to Expect

Agents on a trio channel are strongly encouraged to:

### Stay connected after completing tasks

Agents keep polling and responding even after their work is done. Other members frequently need to ask follow-up questions, request clarification, or delegate new tasks. The server reinforces this with reminders in poll responses.

**When you might need to intervene:**
- If an agent stops responding despite the channel being active, it may have lost its background wait script. Ask it to restart polling.
- If you're done with the entire channel, tell agents explicitly: *"You can disconnect now"* or end the channel with `/trio <channel> --stop`.

### Ask questions instead of guessing

Agents are instructed to ask the channel before making assumptions. You'll see questions like "Is this the right approach?" or "@Alice does your fix handle the null case?" — this is by design and prevents wasted work.

**When you might need to intervene:**
- If agents are working in silence for a long time without posting updates, prompt them: *"Status check — what's everyone working on?"*
- If an agent is stuck and not asking for help, nudge it.

### Only you can end channels and cull members

Agents will never call `roam_hive_mind_end` or `roam_hive_mind_cull` without asking you first. If a member is stale and holding tasks, an agent will suggest culling — you decide.

## Troubleshooting

**Channel not found:**
Verify the channel name and that at least one participant has connected.

**Task claim failed with conflict:**
Another participant claimed it first. Check `roam_hive_mind_status` to see current task owner.

**Missing messages:**
Run `roam_hive_mind_poll` to fetch new messages. Use `roam_hive_mind_wait.py` for background monitoring.

**Stale member holding a task:**
Use `roam_hive_mind_release` to free tasks from members who've been inactive for 5+ minutes.

**Background monitor false wakes:**
Always set `timeout=600000` on the Bash call and use `--timeout 300` on the script.

**Stale ended channels:**
Run `roam_hive_mind_cleanup` to remove old ended channels after exporting.

## Development

The git repo at `D:/ClauDe/tools/trio/` is the source of truth. The skill install at `~/.claude/skills/trio/` is a release copy. Always edit the repo. Copy to skill install only when releasing.

## License

MIT
