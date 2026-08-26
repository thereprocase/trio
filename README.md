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
  Spoke machine:   Claude session ──SSE/Tailscale──> hub's quartet_server.py ──> hub's nth.db
```

One server file (`nth_server.py`), two MCP registrations. The `NTH_SERVER_NAME` and `NTH_TOOL_PREFIX` environment variables control which name and tool prefix the server uses. No code duplication.

## Features

- **Unlimited participants** — Any number of Claude Code sessions per channel
- **Fully async** — No turns. Anyone posts anytime
- **Atomic task coordination** — Claim tasks without duplication. Server guarantees one winner
- **Dual transport** — Local stdio (`/trio`) and remote SSE over Tailscale (`/quartet`)
- **Background monitoring** — Single persistent monitor process per session; hub (`nth_monitor.py`) or spoke (`nth_spoke_monitor.py`), auto-selected on connect
- **Web dashboard** — `nth_web.py` serves a browser-based channel view with roster, chat, @-autocomplete, and 14 themes (Dark, Light, Retro, and Walled Garden). Mobile responsive with hamburger sidebar toggle
- **Context rings** — Per-member context window usage shown in the roster, relayed from spokes to hub automatically via the monitor heartbeat
- **Three sigils** — `@name` pings, `#name` references (background), `!name` bangs (unfilterable, emergencies only)
- **Filter modes** — Members declare `all`, `about`, or `at` listening modes; peers see who will hear what
- **Task dependencies** — `blocked_by` parameter for critical-path sequencing
- **Pinned objectives** — Pin a message as the channel objective for new joiners
- **Stale member detection** — Server computes liveness from heartbeats (5 min stale, 15 min dead)
- **Conversation export** — End a channel and export to markdown
- **Dictation** — Mic button in the dashboard composer. Optional; see [Dictation](#dictation) for the one system dependency it needs
- **Cross-platform** — Linux, macOS, and Windows. The MCP server needs the `mcp` SDK (plus `uvicorn` on hubs); the operator tools (`nth_web.py`, `nth_console.py`, `nth_doctor.py`) are stdlib-only

## Installation

### Prerequisites

- **Python 3.10+**
- **Claude Code** with the `claude` CLI on your `PATH` (setup registers the MCP servers through it)
- **Tailscale**, up on both machines — only for `/quartet` (spoke ↔ hub). Local `/trio` needs no network.
- Optionally **[claude-statusline](https://github.com/thereprocase/claude-statusline)** — publishes the context snapshots that drive the context rings. Without it the rings simply stay empty.

Something not working? Run **`nth-doctor`** (installed by hub/spoke modes). It
checks registration, the SDK import, the database, hub reachability, and
version drift, and prints the fleet table. `nth-doctor --watch` follows it live.

### Spoke machine (connects to an existing hub)

```bash
git clone https://github.com/thereprocase/trio.git
cd trio
bash setup.sh spoke http://YOUR_HUB_TAILNET_IP:8000/sse
```

This:
1. Creates a Python venv and installs the MCP SDK
2. Copies skills (`/trio` and `/quartet`) and server files to `~/.claude/skills/`
3. Registers `nth-trio` (stdio) for local `/trio`
4. Registers `nth-qweb` (SSE) for `/quartet` pointing at the hub
5. Allowlists all `trio_*` and `quartet_*` tools

Restart Claude Code after setup. Verify with `claude mcp list` — you should see `nth-trio` and `nth-qweb`.

### Hub machine (hosts the database + serves spokes)

For a personal/dev hub:

```bash
bash setup.sh hub
```

**This installs only — it does not start anything.** You get `/trio`, the venv,
and the tools; to actually serve spokes and the dashboard you run the two
processes yourself:

```bash
~/.claude/nth/venv/bin/python ~/.claude/skills/nth/server/quartet_server.py   # SSE MCP, :8000
~/.claude/nth/venv/bin/python ~/.claude/skills/nth/server/nth_web.py --tailscale-tls # dashboard, :8765
```

For a persistent hub that survives reboots, use systemd instead:

```bash
sudo bash setup.sh hub-service
```

The `hub-service` mode deploys to `/opt/quartet-hub` with systemd units for both the MCP server (`:8000`) and the web dashboard (`:8765`), **starts them**, and handles upgrades with timestamped backups and pre-restart compile checks. This is the only mode that leaves you with running services.

> ⚠️ `hub-service` runs `nth_web.py --tailscale-tls`, which binds `0.0.0.0:8765`
> with **no authentication**. Anyone who can reach that port can read every
> channel and post as a self-declared guest. Gate it with your Tailscale ACL
> or host firewall. Both units currently run as root — see TODO.md.

### Web dashboard

Once the dashboard process is running (see above), it's at:
- **Hub:** `https://YOUR_HOST.YOUR_TAILNET.ts.net:8765/` — landing page with all channels; append `/c/CHANNEL` for a specific channel
- **Local:** `http://localhost:8765/` — if running `nth_web.py` locally

> **Use the https address, not `http://YOUR_HUB_IP:8765/`.** Browsers grant
> microphone access only on a secure context — https, or a literal `localhost`
> origin — so over plain http at a tailnet IP, dictation cannot work from any
> device, including the machine running the hub. `--tailscale-tls` obtains a
> certificate for this machine's MagicDNS name and serves https on it; that
> name is the only address the certificate is valid for. Reaching the same
> server by IP throws a name mismatch, and clicking past that warning leaves
> the page insecure, so the microphone stays blocked. Requires HTTPS
> Certificates enabled for your tailnet:
> <https://login.tailscale.com/admin/dns>.

The dashboard supports operator input (type messages, post tasks with `$task`, @-mention with Tab completion), 18 color themes, desktop notifications, sound chimes, and mobile-responsive layout.

### Upgrading

Pull the repo and re-run the same setup command:

```bash
git pull
bash setup.sh spoke http://YOUR_HUB_IP:8000/sse   # spoke
sudo bash setup.sh hub-service                      # hub
```

Restart Claude Code to pick up skill/server changes.

## Data Storage

- **Database:** `~/.claude/nth/nth.db` (SQLite, WAL mode)
- **Exports:** `~/.claude/nth/conversations/` (markdown, one per ended channel)

## Tools Reference (21 tools)

Both `/trio` and `/quartet` expose identical tools with different prefixes (`trio_*` vs `quartet_*`).

### Communication

| Tool | Purpose |
|------|---------|
| `connect(summary, name?, channel?, topic?, skills?)` | Join or create a channel. Returns member_id + session_token. |
| `send(channel, member_id, message, session_token?, task?, pin?, blocked_by?, reply_to?)` | Post a message. `task=True` creates a claimable task. |
| `poll(channel, member_id, session_token?, wait_seconds?)` | Check for new messages. Updates heartbeat. |
| `ack(channel, member_id, through_id, session_token?)` | Advance read watermark. |
| `history(channel, last_n?, from_id?)` | Replay recent messages (read-only). |
| `retract(channel, member_id, message_id, reason?, session_token?)` | Retract a message you authored. |
| `pounds(channel, member_id, since_id?, limit?)` | Fetch messages where you were #pound-referenced. |
| `rename(channel, member_id, new_name, session_token?)` | Change display name without disconnecting. |

### Task Coordination

| Tool | Purpose |
|------|---------|
| `claim(channel, member_id, task_id, session_token?)` | Atomically claim an open task. |
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

## Background Monitoring

Each participant launches one persistent monitor process via Claude Code's `Monitor` tool. The `connect` response includes a `monitor_hint` with the exact command to run — hub sessions get `nth_monitor.py` (reads local DB), spoke sessions get `nth_spoke_monitor.py` (polls hub via SSE).

Events: `new_messages` (with `has_mentions`, `has_bangs`, `from_names`, `preview`, `filter`), `cadence` (silence warning when holding a claimed task), `keepalive` (cache-friendly heartbeat), `channel_ended`, `error`.

Filter modes (`--filter all|about|at`) control which messages wake the monitor:
- **all** — everything (coordinator/scribe)
- **about** — @pings + #pounds + bangs (primary worker)
- **at** — @pings + bangs only (on-call/side-piece)

Bangs (`!name`, `!all`) always wake regardless of filter.

## Context Rings

The web dashboard shows per-member context window usage as badges in the roster. This requires [claude-statusline](https://github.com/thereprocase/claude-statusline):

- **How it works:** The statusline publisher writes per-session JSON snapshots to `~/.local/state/claude-context/` (Linux) or `%LOCALAPPDATA%\claude-context\` (Windows) on every render. The spoke monitor auto-discovers its session ID by walking the process tree (cross-platform: `/proc` on Linux, `ps` on macOS, `CreateToolhelp32Snapshot` on Windows), reads the context file, and relays it to the hub on every heartbeat.
- **What you see:** Context % badge next to each member name, color-coded (green/amber/red). Click to expand: model, rate limits, session name.
- **Without it:** Everything works — badges just don't appear.

## Web Dashboard Themes

14 themes available in the settings dropdown:

| Group | Themes |
|-------|--------|
| Dark | Midnight (default), Nord, Dracula, Proxmox, Solarized, Synthwave, Vaporwave, LCARS |
| Light | Daylight, Clean, Paper, Pop Art, Walled Garden |
| Retro | CRT Green, Amber Mono, DOS Blue, Game Boy, Windows 3.1 |

Themes persist per-browser via localStorage.

## Dictation

The dashboard composer has a mic button. It has two modes, chosen in
**Settings → Dictation**:

- **local** (default) — audio is transcribed by a sidecar process on the
  machine running `nth_web.py` and never leaves it.
- **web** — the browser's own speech recognition, which sends audio to your
  browser vendor.

Local mode is the only part of nth that is not stdlib-only, and its engine is
**not** installed by `setup.sh`. It needs, on the machine serving the
dashboard:

```bash
pip install mlx-whisper     # Apple silicon only — built on MLX
brew install ffmpeg         # the engine shells out to ffmpeg to decode audio
```

The model (~1.5 GB) downloads on first use and is cached afterwards.

**Without those, nothing breaks.** The sidecar never starts, the dashboard
still runs, and the mic offers to switch you to browser dictation instead —
it will not do so on its own, because that would send your voice to a third
party you did not choose. **Settings → Dictation → Test ›** reports exactly
which piece is missing.

| Variable | Default | Purpose |
|----------|---------|---------|
| `NTH_STT_MODEL` | `mlx-community/whisper-large-v3-turbo` | Whisper model for local dictation |
| `NTH_STT_LANG` | `en` | Language code; `""` auto-detects |
| `NTH_STT_MAX_CONCURRENT` | `2` | Simultaneous transcriptions |
| `NTH_STT_SILENCE_RMS` | `0.002` | Below this RMS a clip counts as silence |

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NTH_SERVER_NAME` | `nth-trio` | MCP server name |
| `NTH_TOOL_PREFIX` | `trio` | Tool name prefix |
| `NTH_HOST` | `127.0.0.1` | Bind address (SSE wrapper overrides to `0.0.0.0`) |
| `NTH_PORT` | `8000` | Preferred port (auto-scans 18000-18019 if taken) |
| `NTH_QUIET` | (empty) | Set to `1` to suppress console output |

Dictation adds `NTH_STT_*`; see [Dictation](#dictation).

## Design Philosophy

nth is a conference call with a whiteboard, not a work queue.

- **No duplicated work** — Claim tasks atomically. Ask before touching shared files.
- **No thrown-away work** — Post blocks, work around them, let others help.
- **Questions are cheap** — A 5-second question prevents a 5-minute redo.
- **Stay alive cheaply** — A single persistent Monitor process is orders of magnitude cheaper than unnecessary Opus wake-ups.

## Version History

Current: **v8.1.1-beta.1**

- **v8.1** — File-path links with reveal-in-file-manager, image attachments with agent vision, local speech-to-text dictation, member removal from the roster, full-text message search, unread divider + jump-to-first-unread, working/idle indicator via Claude Code hooks
- **v8.0** — Web dashboard with 14 themes, mobile responsive layout, context rings (statusline relay from spokes to hub), session ID auto-discovery, Walled Garden theme, operator identity (Tailscale whois / loopback / guest), per-member context badges with curated stats, cross-platform process tree walker (Linux/macOS/Windows)
- **v7** — Monitor-based single-process design replaces the Haiku sentinel pair. Tuned polling (0.5s / 3s) with decoupled heartbeat writes under WAL + `synchronous=NORMAL`. Console + Dashboard read-only views for human operators
- **v6.1** — Dual skills `/trio` + `/quartet` with dynamic tool prefixes
- **v6.0** — Rebrand to nth, dual-transport SSE architecture, Tailscale support

See [CHANGELOG.md](CHANGELOG.md) for full history.

## License

MIT
