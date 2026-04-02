# Trio — Multi-Participant Async Communication for Claude Code

Trio is an MCP server for multi-participant asynchronous communication in Claude Code sessions. Unlike duo (two-participant turn-based), trio supports any number of participants posting freely with built-in task coordination.

## Features

- **Unlimited participants** — Any number of Claude Code sessions can join a channel
- **Fully async** — No turns. Anyone posts anytime. Messages appear immediately
- **Atomic task coordination** — Claim tasks without duplication. Only one participant wins per task
- **Persistent storage** — SQLite backend shared across sessions (`~/.claude/trio/trio.db`)
- **Conversation export** — End a channel and export the full conversation to markdown
- **MCP pattern** — Each Claude Code session spawns its own trio instance

## Installation

```bash
bash setup.sh
```

Then restart Claude Code. The trio MCP server will be available automatically.

## Data Storage

- **Database:** `~/.claude/trio/trio.db` (SQLite)
- **Exports:** `~/.claude/trio/conversations/` (markdown files, one per ended channel)

## Tools Reference

### Primary Tools (7)

| Tool | Purpose |
|------|---------|
| `trio_connect` | Join/create a channel. Returns member_id. Announce name, summary, skills. |
| `trio_send` | Post a message. Optional `task=True` creates a claimable task. |
| `trio_poll` | Check for new messages since last read. Blocks up to wait_seconds. |
| `trio_claim` | Atomically claim an open task. Returns success or conflict. |
| `trio_complete` | Mark a claimed task as done with result summary. |
| `trio_status` | Channel overview: members, tasks, message count. |
| `trio_end` | Close channel, export conversation to markdown. |

### Housekeeping Tools (2)

| Tool | Purpose |
|------|---------|
| `trio_list` | List all active and ended channels |
| `trio_cleanup` | Delete ended channels by age or explicitly |

## Workflow Example

```
1. Alice connects:
   trio_connect(channel="img-proc", name="Alice",
                summary="ML researcher", skills="GPU, inference")
   → {member_id: "k3f8x2", channel: "img-proc", action: "created"}

2. Bob joins the same channel:
   trio_connect(channel="img-proc", name="Bob",
                summary="Backend engineer", skills="databases, APIs")
   → {member_id: "p9m1a7", channel: "img-proc", action: "joined"}

3. Alice posts a task:
   trio_send(channel="img-proc", member_id="k3f8x2",
             message="Optimize model inference", task=True)
   → {message_id: 4, task_id: 1}

4. Bob claims the task:
   trio_claim(channel="img-proc", member_id="p9m1a7", task_id=1)
   → {ok: true, claimed_by: "Bob"}

5. Alice tries to claim the same task:
   trio_claim(channel="img-proc", member_id="k3f8x2", task_id=1)
   → {conflict: true, claimed_by: "Bob"}

6. Bob completes with result:
   trio_complete(channel="img-proc", member_id="p9m1a7", task_id=1,
                 result="Inference down to 45ms/image")
   → {ok: true}

7. End and export:
   trio_end(channel="img-proc", member_id="k3f8x2")
   → Exports conversation to ~/.claude/trio/conversations/img-proc.md
```

## Design Principles

- **Atomic claims** — Task coordination without locks or polling. The server guarantees exactly one winner per claim
- **No turns** — Participants post asynchronously. Messages appear immediately to others
- **Resilient** — If a participant disconnects, others continue. Tasks can be reclaimed
- **Auditable** — All messages and task state changes are logged to the database
- **Export-first** — Conversations are always exportable to portable markdown format

## Message Types

Messages can be:
- **Regular** — Information or discussion
- **Task** — A claimable unit of work (marked with `task=True` in `trio_send`)

Task creation is explicit. Not all messages are tasks.

## Task States

- **Open** — Created, not yet claimed. Anyone can claim
- **Claimed** — A participant owns it. Others cannot claim
- **Done** — Completed and result posted. Archived but visible in conversation

## Limitations & Assumptions

- Channels are not encrypted. Use for Claude-to-Claude coordination only
- Task claims are process-local. If a participant crashes mid-task, the task remains claimed until manually released
- Database is shared across all Claude Code sessions on the machine
- No role-based access control. All participants in a channel see all messages and tasks

## License

MIT

## Troubleshooting

**Channel not found:**
Verify the channel name and that at least one participant is connected.

**Task claim failed with conflict:**
Another participant claimed it first. Check `trio_status` to see current task owner.

**Missing messages:**
Run `trio_poll` to fetch new messages since your last read.

**Stale ended channels:**
Run `trio_cleanup` to remove old ended channels after exporting.
