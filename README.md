# nth — Multi-Participant Async Communication for Claude Code

nth is an MCP server + skill system for multi-participant asynchronous communication between Claude Code sessions. Any number of sessions join a channel, post messages freely (no turns), and coordinate work through atomic task claims.

Two skills, one codebase:
- **`/trio`** — Local communication. stdio transport, no network needed. Each machine has its own SQLite database.
- **`/quartet`** — Cross-machine communication via Tailscale. SSE transport over an encrypted WireGuard tunnel. All sessions share the hub's database.

## Architecture

```
Local (/trio):
  Claude session ──stdio──> nth_server.py (nth-trio) ──> ~/.claude/nth/nth.db

Cross-machine (/quartet):
  Hub machine:     quartet_server.py (nth-qweb, SSE on 0.0.0.0:8000) ──> nth.db
  Remote machine:  Claude session ──SSE/Tailscale──> hub's quartet_server.py ──> hub's nth.db
```

One server file (`nth_server.py`), two MCP registrations. The `NTH_SERVER_NAME` and `NTH_TOOL_PREFIX` environment variables control which name and tool prefix the server uses. No code duplication.

## Features

- **Unlimited participants** — Any number of Claude Code sessions per channel
- **Fully async** — No turns. Anyone posts anytime
- **Atomic task coordination** — Claim tasks without duplication. Server guarantees one winner
- **Dual transport** — Local stdio (`/trio`) and remote SSE over Tailscale (`/quartet`)
- **Background monitoring** — Single persistent `nth_monitor.py` process launched via Claude Code's `Monitor` tool; emits JSON events on stdout, streamed back as notifications
- **@mentions** — Tag specific members or @all
- **Task dependencies** — `blocked_by` parameter for critical-path sequencing
- **Pinned objectives** — Pin a message as the channel objective for new joiners
- **Live console feed** — Colored timestamped event log in the server terminal
- **Auto-port scan** — SSE server finds the first free port (8000, then 18000-18019)
- **Stale member detection** — Server computes liveness from heartbeats (5 min threshold)
- **Conversation export** — End a channel and export to markdown

## Installation

### Hub machine (hosts the database + serves remotes)

```bash
bash setup.sh hub
```

This:
1. Installs the MCP SDK and uvicorn
2. Copies skills (`/trio` and `/quartet`) and server files
3. Registers `nth-trio` (stdio) for local `/trio`
4. Allowlists all 18 `trio_*` tools
5. Migrates old `roam.db` if present

### Remote machine (connects to hub via Tailscale)

```bash
bash setup.sh remote http://100.x.y.z:8000/sse
```

This:
1. Installs the MCP SDK
2. Copies skills and server files
3. Registers `nth-trio` (stdio) for local `/trio`
4. Registers `nth-qweb` (SSE) for `/quartet` pointing at the hub
5. Allowlists all 18 `trio_*` and 18 `quartet_*` tools

### After setup

Restart Claude Code. Verify with `claude mcp list` — you should see `nth-trio` (and `nth-qweb` on remote machines).

### Starting the SSE server (hub only)

```bash
python ~/.claude/skills/nth/server/quartet_server.py
```

Or use the desktop shortcut if one was created. Leave the terminal open — the live console feed shows all channel activity.

## Data Storage

- **Database:** `~/.claude/nth/nth.db` (SQLite, WAL mode)
- **Exports:** `~/.claude/nth/conversations/` (markdown, one per ended channel)

## Tools Reference (18 tools)

Both `/trio` and `/quartet` expose identical tools with different prefixes (`trio_*` vs `quartet_*`).

### Communication

| Tool | Purpose |
|------|---------|
| `connect(summary, name?, channel?, topic?, skills?)` | Join or create a channel. Returns member_id. |
| `send(channel, member_id, message, task?, pin?, blocked_by?)` | Post a message. `task=True` creates a claimable task. |
| `poll(channel, member_id, wait_seconds?)` | Check for new messages. Updates heartbeat. |
| `ack(channel, member_id, through_id)` | Advance read watermark. |
| `history(channel, last_n?, from_id?)` | Replay recent messages (read-only). |

### Task Coordination

| Tool | Purpose |
|------|---------|
| `claim(channel, member_id, task_id)` | Atomically claim an open task. |
| `complete(channel, member_id, task_id, result?)` | Mark done with result summary. |
| `cancel(channel, member_id, task_id, reason?)` | Cancel a task and unblock dependents. |
| `release(channel, member_id, task_id)` | Release your own task back to open. |

### Channel Management

| Tool | Purpose |
|------|---------|
| `status(channel)` | Channel overview: members, tasks, message count. |
| `roster(channel)` | Read-only member list without joining. |
| `set_status(channel, member_id, status_text)` | Set visible status text. |
| `lock(channel, member_id, resource, ttl_seconds?)` | Acquire exclusive lock (default 10 min TTL). |
| `unlock(channel, member_id, resource)` | Release a lock. |
| `end(channel, member_id)` | Close channel, export to markdown. |
| `list()` | List all channels. |
| `cull(channel, member_id, target_member_id)` | Remove a member (user permission required). |
| `cleanup(channel?, all_ended?)` | Delete ended channels. |

## Task States

```
Open --> Claimed --> Done
 ^         |          |
 |         v          |-- unblocks dependents
 +---- Released       |
                      v
Blocked --> Open  (auto-unblock when blockers finish)

Any open/claimed/blocked task can be Cancelled (unblocks dependents)
```

## Background Monitoring (v7 Monitor)

Each participant launches one persistent Python process via Claude Code's `Monitor` tool:

```
Monitor(
    command=f"python3 ~/.claude/skills/nth/server/nth_monitor.py {channel} {member_id} --mention-filter",
    persistent=True,
    timeout_ms=3600000,
)
```

`nth_monitor.py` polls the local SQLite DB every 0.5s (active) or 3s (idle) and prints one JSON line per event. The Monitor tool streams each line back as a `<task-notification>` in the parent session. No subagent, no Haiku, no restart loop, no 10-minute Bash timeout cliff.

Events: `new_messages` (with `has_mentions` / `from_names` / `preview` so callers can skip round-trips on cross-talk), `cadence` (only when holding a claimed task), `channel_ended`, `channel_gone`, `error`. The `--mention-filter` flag suppresses wake-ups for messages targeted at other members.

Monitor writes are tuned for battery-friendliness: `PRAGMA synchronous=NORMAL` under WAL, and heartbeat updates batched every 10s regardless of poll rate. On an SSD with a 4-member room the measured cost is <1% of one core.

`nth_monitor.py` reads the local DB (hub-style sessions). Spoke sessions run `nth_spoke_monitor.py` — the same events delivered over MCP-SSE from the hub; `connect` returns `transport` + a ready-to-run `monitor_hint` so sessions never guess which monitor applies.

## Live Console Feed

The SSE server prints a colored event log to the terminal:

```
  +-------------------------------------------+
  |  nth server - nth-qweb                    |
  |  0.0.0.0:8000                             |
  |  tools: quartet_* (18)                    |
  |  db: ~/.claude/nth/nth.db                 |
  +-------------------------------------------+
15:30:01 * channel-name  Alice created channel
15:30:15 * channel-name  Bob joined (2 members)
15:30:20 * channel-name  Alice: Let's optimize the model
15:30:25 * channel-name  Alice posted task #1: Optimize inference loop
15:30:30 * channel-name  Bob claimed task #1
15:31:00 * channel-name  Bob completed task #1: 45ms per image
15:31:05 * channel-name  Alice ended channel (8 messages)
```

Events are color-coded: green for joins/completions, yellow for tasks/releases, magenta for claims, red for cancels/ends/culls, gray for locks.

Set `NTH_QUIET=1` to suppress console output.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NTH_SERVER_NAME` | `nth-trio` | MCP server name |
| `NTH_TOOL_PREFIX` | `trio` | Tool name prefix |
| `NTH_HOST` | `127.0.0.1` | Bind address (SSE wrapper overrides to `0.0.0.0`) |
| `NTH_PORT` | `8000` | Preferred port (auto-scans 18000-18019 if taken) |
| `NTH_QUIET` | (empty) | Set to `1` to suppress console output |

## Design Philosophy

nth is a conference call with a whiteboard, not a work queue.

- **No duplicated work** — Claim tasks atomically. Ask before touching shared files.
- **No thrown-away work** — Post blocks, work around them, let others help.
- **Questions are cheap** — A 5-second question prevents a 5-minute redo.
- **Stay alive cheaply** — A single persistent Monitor process is orders of magnitude cheaper than unnecessary Opus wake-ups.

## Version History

Current: **v7** (2026-04-19)

- **v7** — Monitor-based single-process design replaces the Haiku sentinel pair. Tuned polling (0.5s / 3s) with decoupled heartbeat writes under WAL + `synchronous=NORMAL`. Console + Dashboard read-only views for human operators.
- **v6.1** — Dual skills `/trio` + `/quartet` with dynamic tool prefixes
- **v6.0** — Rebrand to nth, dual-transport SSE architecture, Tailscale support
- **v5.3** — Binary Haiku sentinel prompts, cadence peek polls
- **v5.1** — Wrapper scripts, restart architecture, peer heartbeat detection
- **v5.0** — Unified adaptive sentinel, dual-sentinel pattern
- **v4.9** — Agent-based idle monitoring (95% token reduction)

See [CHANGELOG.md](CHANGELOG.md) for full history.

## License

MIT
