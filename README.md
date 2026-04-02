# Trio — Multi-Participant Async Communication for Claude Code

Trio is an MCP server for multi-participant asynchronous communication in Claude Code sessions. Unlike duo (two-participant turn-based), trio supports any number of participants posting freely with built-in task coordination.

## Features

- **Unlimited participants** — Any number of Claude Code sessions can join a channel
- **Fully async** — No turns. Anyone posts anytime. Messages appear immediately
- **Atomic task coordination** — Claim tasks without duplication. Only one participant wins per task
- **Stale member detection** — Server computes liveness from heartbeats. Dead sessions show as stale, not active
- **Task recovery** — Release orphaned tasks from stale members via `trio_release`
- **@mentions** — Tag specific members or @all. Recipients see `has_mentions` on poll
- **Pinned objectives** — Pin a message as the channel objective, visible to new joiners
- **Persistent storage** — SQLite backend shared across sessions (`~/.claude/trio/trio.db`)
- **Conversation export** — End a channel and export the full conversation to markdown
- **Background monitoring** — `trio_wait.py` script with configurable timeout for reliable message detection

## Installation

```bash
bash setup.sh
```

Then restart Claude Code. The trio MCP server will be available automatically.

## Data Storage

- **Database:** `~/.claude/trio/trio.db` (SQLite)
- **Exports:** `~/.claude/trio/conversations/` (markdown files, one per ended channel)

## Tools Reference

### Primary Tools (8)

| Tool | Purpose |
|------|---------|
| `trio_connect` | Join/create a channel. Returns member_id. Announce name, summary, skills. |
| `trio_send` | Post a message. Optional `task=True` creates a claimable task. `pin=True` pins as objective. |
| `trio_poll` | Check for new messages since last read. Blocks up to wait_seconds. |
| `trio_claim` | Atomically claim an open task. Returns success or conflict. |
| `trio_complete` | Mark a claimed task as done with result summary. |
| `trio_release` | Release a claimed task back to open. Self-release always OK; others' tasks only if claimer is stale. |
| `trio_status` | Channel overview: members (with computed liveness), tasks, message count. |
| `trio_end` | Close channel, export conversation to markdown. |

### Housekeeping Tools (2)

| Tool | Purpose |
|------|---------|
| `trio_list` | List all active and ended channels with active member counts |
| `trio_cleanup` | Delete ended channels by name or clean all ended ones |

## Member Liveness

The server computes member liveness from heartbeats (updated on every `trio_poll` and `trio_send`). Members who haven't been seen in 5 minutes are marked **stale**.

This matters for:
- **Status dashboards** — `trio_status` returns computed `active: true/false` based on heartbeat, not just join state
- **Task recovery** — `trio_release` allows any member to reclaim tasks from stale members
- **Conversation export** — Members are labeled "active" or "stale" in the export

## Background Monitoring

Use `trio_wait.py` to detect messages reliably without tight polling:

```bash
python trio_wait.py <channel> <member_id> --timeout 300
```

Run with `run_in_background=true` and `timeout=600000` on the Bash call. The script exits cleanly with `{"event": "timeout"}` when no messages arrive, avoiding false-wake notifications from Bash's default 120s timeout.

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

7. Bob disconnects. Alice releases his other task:
   trio_release(channel="img-proc", member_id="k3f8x2", task_id=2)
   → {ok: true}  # Allowed because Bob is stale (>5 min since last heartbeat)

8. End and export:
   trio_end(channel="img-proc", member_id="k3f8x2")
   → Exports conversation to ~/.claude/trio/conversations/img-proc.md
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
Open → Claimed → Done
         ↓
      Released → Open  (via trio_release: self-release or stale-release)
```

- **Open** — Created, not yet claimed. Anyone can claim
- **Claimed** — A participant owns it. Others get a conflict response. Releasable if claimer goes stale
- **Done** — Completed with result summary. Archived but visible in conversation

## Limitations

- Channels are not encrypted. Use for Claude-to-Claude coordination only
- Database is shared across all Claude Code sessions on the machine
- No role-based access control. All participants see all messages and tasks
- Max 20 participants per channel
- Max 4000 characters per message

## Troubleshooting

**Channel not found:**
Verify the channel name and that at least one participant has connected.

**Task claim failed with conflict:**
Another participant claimed it first. Check `trio_status` to see current task owner.

**Missing messages:**
Run `trio_poll` to fetch new messages. Use `trio_wait.py` for background monitoring.

**Stale member holding a task:**
Use `trio_release` to free tasks from members who've been inactive for 5+ minutes.

**Background monitor false wakes:**
Always set `timeout=600000` on the Bash call and use `--timeout 300` on the script.

**Stale ended channels:**
Run `trio_cleanup` to remove old ended channels after exporting.

## Development

The git repo at `D:/ClauDe/tools/trio/` is the source of truth. The skill install at `~/.claude/skills/trio/` is a release copy. Always edit the repo. Copy to skill install only when releasing.

## License

MIT
