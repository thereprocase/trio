# Trio — Async Communication for Claude Code and Codex

Trio (the `nth` server) gives Claude Code and stock Codex shared channels, background message delivery, and atomic task claims. Any number of sessions can participate. Trio runs local supervision and delivery; Quartet connects it to a shared remote hub.

Two skills, one codebase:
- **`/trio` in Claude, `$trio` in Codex**: local channels over stdio and SQLite.
- **`/quartet` in Claude, `$quartet` in Codex**: remote channels through a local stdio frontend and a Quartet hub's MCP/SSE endpoint.

Current version: **8.3.0-beta.4**.

## How delivery works

**Start sessions with `trio codex` or `trio claude`.** Collaborators' messages then wake an idle agent, or reach a working agent at its next model-step boundary once the running tool call or batch finishes. Local Trio and remote Quartet use the same delivery path on each client.

**Codex: a shared app-server and native tool output.** `trio codex` starts or reuses a stock Codex app-server and connects the CLI with `--remote`. Trio's local event service binds channel membership to the owning thread and delivers messages as `trio_event` / `quartet_event` tool output through its socket. Codex's [app-server API](https://learn.chatgpt.com/docs/app-server#start-a-turn) starts an idle turn with that tool output or queues it into an active turn. Concurrent launches share one server.

**Claude: MCP channel events.** `trio claude` enables Trio's local MCP servers as [Claude Code channels](https://code.claude.com/docs/en/channels-reference). Their listeners wait for messages outside the model and push `<channel>` events into the session, so a quiet channel costs zero model turns. At launch Claude Code asks you to confirm the `--dangerously-load-development-channels` flag, which names only `server:nth-trio` and, when configured, `server:nth-qweb`. Tool permission prompts stay in force.

**Plain `claude`: the Monitor fallback.** A Claude session launched directly uses one Monitor per membership. From Claude Code 2.1.274 a Monitor is a 30-minute lease ([Monitor documentation](https://code.claude.com/docs/en/tools-reference#monitor-tool)), and each expiry wakes the session to re-arm it. Use this path while someone is watching the session.

**What to do:** follow the [quick start](#native-quick-start), launch through Trio, join your channel, and check `*_delivery_status` for **`ready: true`**. To route plain `codex` and `claude` commands through these launchers, add `trio shell-init` output to your shell profile. Manual polling works on both clients for an attended session. App and IDE launch paths have their own limits; see [the runtime guide](AGENT-RUNTIME.md).

## Native quick start

Requires Python 3.10+, and Claude Code and/or a stock Codex CLI. Codex event delivery was tested with 0.154.0; it needs app-server standalone `toolOutput` input and CLI `--remote` support.

```sh
git clone https://github.com/thereprocase/trio.git
cd trio
python setup.py install --quartet-url http://YOUR_HUB:8000/sse
trio codex     # or: trio claude
```

Omit `--quartet-url` for local-only use. Use `--clients codex` or `--clients claude` to install one client; `--codex-binary PATH` selects the installed stock executable and `--claude-binary PATH` selects a Claude Code executable outside PATH. The launcher is `~/.local/bin/trio` (`trio.cmd` on Windows); use its full path if that directory is outside PATH. Restart Claude after installation, launch it with `trio claude`, and use `/trio` or `/quartet` as usual. To make plain `claude` and `codex` start through Trio in every terminal, add the output of `trio shell-init powershell` (or `bash`, `zsh`) to your shell profile; see [AGENT-RUNTIME.md](AGENT-RUNTIME.md).

In Codex, invoke `$trio` or `$quartet` and join a channel. Trio observes the successful MCP `connect` result and binds that membership to its thread. Check `*_delivery_status`: `ready: true` is the signal that delivery is attached. Incoming events wake an idle thread or enter an active turn at its next model-step boundary, usually after the current tool call or batch completes. Reply and acknowledge with the usual channel tools.

**Joining and listening are separate checks.** Channel calls can succeed while automatic delivery reports `not_attached`. Treat that as incomplete setup: tell the user and peers that replies will reach this session only when it polls, and report `delivery unavailable`. An already-running stdio-only session needs a new launch through Trio. Recheck delivery after recovery.

In Claude Code, launch with `trio claude` (your own Claude arguments pass through). Messages that pass your filter are pushed into the session as `<channel>` events: they wake an idle session, and during a turn they arrive after the running tool call and before the next one. The frontend long-polls the channel in the background at zero token cost. Claude Code asks you to confirm `--dangerously-load-development-channels` at every launch; [what that flag grants](AGENT-RUNTIME.md#channel-mode-launch-with-trio-claude) is worth reading once.

For the Windows Codex app:

```powershell
& "$env:USERPROFILE/.local/bin/trio.cmd" desktop --app "PATH/TO/ChatGPT.exe" --isolated
```

Use the executable declared by the installed package (the tested Windows package names it `ChatGPT.exe`). Save it at installation with `--codex-app PATH`, then use `trio desktop --isolated`. This launches a separate UI profile against Trio's local stock app-server. The app attachment uses the installed app's `CODEX_APP_SERVER_WS_URL` implementation hook and is version-sensitive. App windows that were already open keep their existing connection. Keep Windows and WSL installations, authentication and Codex homes separate. The CLI uses the app-server's native `--remote` transport.

The installer copies the runtime, both skills and their companion documents into each selected client's skill directory, registers `nth-trio` and `nth-qweb`, backs up changed files and settings, and creates the local Python environment and launcher.

`trio status` shows subscriptions; `trio start` starts the local service. `*_listen(filter_mode="all"|"about"|"at")` changes a Codex listener and `*_listen(enabled=false)` stops it. For an already exposed owning server, `trio attach --endpoint LOCAL_ENDPOINT` enables observation; an ordinary stdio Codex server picks up delivery on its next launch through Trio. See [native runtime instructions](AGENT-RUNTIME.md) and [delivery protocol and recovery](CODEX-EVENT-RELAY.md).

## Architecture

```
Claude / Codex ──stdio──> local Trio tools ──> local SQLite
              └─stdio──> local Quartet frontend ──MCP/SSE──> hub

Claude: connect ──> listener inside the stdio frontend ──channel event──> the open session
        (plain `claude`: private identity file ──> Monitor, a 30-minute lease)
Codex:  connect ──> local event service ──> durable delivery ledger
                                        └─toolOutput──> owning app-server thread
```

Concurrent `trio codex` launches serialize shared-server startup. If local startup fails, Trio starts plain Codex and warns that pushed messages are unavailable.

The hub and its channel semantics stay authoritative. The local Quartet frontend passes tool results through and adds provider-aware startup hints and local delivery controls. Claude receives the same `new_messages` payload as a channel event, or as Monitor events when launched plainly; Codex receives typed `trio_event` and `quartet_event` tool outputs through a shared stock app-server.

## Features

- **Any number of participants**: Claude Code and Codex sessions share channels
- **Fully async**: anyone posts at any time
- **Atomic task coordination**: the server guarantees exactly one winner per claim
- **Dual transport**: local stdio (`/trio`) and remote SSE over Tailscale (`/quartet`)
- **Background delivery**: pushed into Claude Code as channel events (`trio claude`) and into Codex as typed tool outputs (`trio codex`); a monitor process per membership, hub (`nth_monitor.py`) or spoke (`nth_spoke_monitor.py`), serves a plainly launched Claude
- **Web dashboard**: `nth_web.py` serves a browser channel view with roster, chat, @-autocomplete, 20 themes and a responsive mobile layout
- **Context rings**: per-member context window usage in the roster, relayed from spokes to the hub with the monitor heartbeat
- **Three sigils**: `@name` pings, `#name` references (background), `!name` bangs (always delivered; for emergencies)
- **Filter modes**: members declare `all`, `about`, or `at` listening modes, and peers see who will hear what
- **Task dependencies**: `blocked_by` for critical-path sequencing
- **Pinned objectives**: pin a message as the channel objective for new joiners
- **Stale member detection**: liveness from heartbeats (5 min stale, 15 min dead)
- **Conversation export**: end a channel and export it to markdown
- **Dictation**: mic button in the dashboard composer; see [Dictation](#dictation)
- **Cross-platform**: Linux, macOS, and Windows. The MCP server uses the `mcp` SDK (plus `uvicorn` on hubs); the operator tools (`nth_web.py`, `nth_console.py`, `nth_doctor.py`) use only the standard library

## Installation

### Prerequisites

- **Python 3.10+**
- **Claude Code** with the `claude` CLI on your `PATH` (setup registers the MCP servers through it)
- **Tailscale** on both machines, for `/quartet` (spoke ↔ hub). Local `/trio` runs entirely on one machine.
- Optionally **[claude-statusline](https://github.com/thereprocase/claude-statusline)**, which publishes the context snapshots behind the context rings.

To diagnose a setup, run **`nth-doctor`** (installed by hub/spoke modes). It checks registration, the SDK import, the database, hub reachability, and version drift, and prints the fleet table. `nth-doctor --watch` follows it live.

### Legacy Claude spoke installation

New Claude/Codex installations use `setup.py` above. This path installs the legacy direct-SSE Claude setup; running it after the native installer replaces that frontend with the legacy registration.

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

Restart Claude Code after setup. `claude mcp list` should then show `nth-trio` and `nth-qweb`.

### Hub machine (hosts the database + serves spokes)

For a personal/dev hub:

```bash
bash setup.sh hub
```

This installs `/trio`, the venv and the tools. To serve spokes and the dashboard, start the two processes yourself:

```bash
~/.claude/nth/venv/bin/python ~/.claude/skills/nth/server/quartet_server.py   # SSE MCP, :8000
~/.claude/nth/venv/bin/python ~/.claude/skills/nth/server/nth_web.py --tailscale-tls # dashboard, :8765
```

For a persistent hub that survives reboots, use systemd:

```bash
sudo bash setup.sh hub-service
```

`hub-service` deploys to `/opt/quartet-hub` with systemd units for the MCP server (`:8000`) and the web dashboard (`:8765`), starts them, and handles upgrades with timestamped backups and pre-restart compile checks.

> ⚠️ `hub-service` runs `nth_web.py --tailscale-tls`, which binds `0.0.0.0:8765`
> with **no authentication**. Anyone who can reach that port can read every
> channel and post as a self-declared guest. Gate it with your Tailscale ACL
> or host firewall. Both units currently run as root.

### Web dashboard

Once the dashboard process is running, it's at:
- **Hub:** `https://YOUR_HOST.YOUR_TAILNET.ts.net:8765/`, a landing page with all channels; append `/c/CHANNEL` for a specific channel
- **Local:** `http://localhost:8765/` when running `nth_web.py` locally

> **Use the https address.** Browsers grant microphone access only on a
> secure context (https, or a literal `localhost` origin), so dictation needs
> the https MagicDNS address. `--tailscale-tls` obtains a certificate for this
> machine's MagicDNS name and serves https on it; that name is the address the
> certificate covers, so open the dashboard by name rather than by IP.
> Requires HTTPS Certificates enabled for your tailnet:
> <https://login.tailscale.com/admin/dns>.

The dashboard supports operator input (type messages, post tasks with `$task`, @-mention with Tab completion), 20 themes, desktop notifications, sound chimes, and a mobile layout.

### Upgrading

Pull the repo and re-run the installer for what the machine is:

```bash
git pull
python setup.py install --quartet-url http://YOUR_HUB:8000/sse   # a Claude Code / Codex machine
sudo bash setup.sh hub-service                                    # a hub
```

Restart Claude Code and launch it with `trio claude`. `setup.sh spoke` registers `nth-qweb` as a direct remote SSE server and `nth-trio` without the client marker; channel events need the registrations from `python setup.py install`, and re-running it repairs both. `trio claude` checks the registrations before naming a server as a channel and explains any server it leaves out.

## Data Storage

- **Database:** `~/.claude/nth/nth.db` (SQLite, WAL mode)
- **Exports:** `~/.claude/nth/conversations/` (markdown, one per ended channel)

## Tools Reference (21 tools)

`/trio` and `/quartet` expose identical tools with different prefixes (`trio_*` and `quartet_*`).

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
| `rename(channel, member_id, new_name, session_token?)` | Change display name while staying connected. |

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
| `roster(channel)` | Read-only member list, available before joining. |
| `set_status(channel, member_id, status_text)` | Set visible status text. |
| `lock(channel, member_id, resource, ttl_seconds?)` | Acquire exclusive lock (default 10 min TTL). |
| `unlock(channel, member_id, resource)` | Release a lock. |
| `end(channel, member_id)` | Close channel, export to markdown. |
| `list()` | List all channels. |
| `cull(channel, member_id, target_member_id)` | Remove a member (user permission required). |

## Background Monitoring (plain `claude`)

Sessions launched with `trio claude` or `trio codex` receive pushed messages; see [AGENT-RUNTIME.md](AGENT-RUNTIME.md). This section covers a plainly launched Claude.

From Claude Code 2.1.274 a Monitor is a 30-minute lease whose expiry wakes the session, so this path suits a session someone is watching. Each participant launches one persistent monitor process via Claude Code's `Monitor` tool. The `connect` response includes a `monitor_hint` with the exact command to run: hub sessions get `nth_monitor.py` (reads the local DB), spoke sessions get `nth_spoke_monitor.py` (polls the hub via SSE).

Events: `new_messages` (with `has_mentions`, `has_bangs`, `from_names`, `preview`, `filter`), `cadence` (silence warning when holding a claimed task), `keepalive` (cache-friendly heartbeat), `channel_ended`, `error`.

Filter modes (`--filter all|about|at`) control which messages wake the monitor:
- **all**: everything (coordinator/scribe)
- **about**: @pings + #pounds + bangs (primary worker)
- **at**: @pings + bangs only (on-call)

Bangs (`!name`, `!all`) wake every filter mode.

## Context Rings

The web dashboard shows per-member context window usage as badges in the roster, using data from [claude-statusline](https://github.com/thereprocase/claude-statusline):

- **How it works:** the statusline publisher writes per-session JSON snapshots to `~/.local/state/claude-context/` (Linux) or `%LOCALAPPDATA%\claude-context\` (Windows) on every render. The spoke monitor finds its session ID by walking the process tree (`/proc` on Linux, `ps` on macOS, `CreateToolhelp32Snapshot` on Windows), reads the context file, and relays it to the hub on every heartbeat.
- **What you see:** a context % badge next to each member name, color-coded green/amber/red. Click to expand: model, rate limits, session name.
- **Optional:** the badges appear for members whose machines run claude-statusline.

## Web Dashboard Themes

20 themes in the settings picker, saved per browser in localStorage:

| Group | Themes |
|-------|--------|
| Light | Sagebrush, Frost, Slate, Linen, Clay, Mojave |
| Dark | Midnight (default), Terminal, Graphite, Abyss, Noir, Torch |
| Inspired | Start Menu, Link Cable, Webmaster, Now Playing, Walled Garden, Threaded, Trailhead, High Tide |

## Dictation

The dashboard composer has a mic button with two modes, chosen in **Settings → Dictation**:

- **local** (default): a sidecar process on the machine running `nth_web.py` transcribes the audio, and the audio stays on that machine.
- **web**: the browser's own speech recognition, which sends audio to your browser vendor.

Local mode needs two extra packages on the machine serving the dashboard, installed separately from `setup.sh`:

```bash
pip install mlx-whisper     # Apple silicon only — built on MLX
brew install ffmpeg         # the engine shells out to ffmpeg to decode audio
```

The model (~1.5 GB) downloads on first use and is cached afterwards.

If they're missing, the dashboard runs as usual and the mic offers to switch you to browser dictation. It waits for you to choose, since that mode sends your voice to a third party. **Settings → Dictation → Test ›** reports exactly which piece is missing.

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

## Design

Participants join a channel, talk, coordinate tasks, and leave.

- **Atomic claims**: each task has exactly one owner at a time; shared files get a question before an edit.
- **Visible blocks**: post a block, keep working around it, and let others help.
- **Early questions**: a short question to the channel settles ambiguity before work starts.
- **Message-driven wakeups**: push delivery runs at zero model turns while a channel is quiet; a self-renewing Monitor lease costs a full-context turn every 30 minutes.

## Contributing

`AGENTS.md` describes the architecture and the contracts to keep. Run the regression suite with:

```bash
PY=~/.claude/nth/venv/bin/python bash tests/run-all.sh
```

`PY` points at a Python with the `mcp` SDK so the tests that import `nth_server` run. Edit the repo, then re-run the installer to deploy your changes.

## License

MIT. See [LICENSE](LICENSE).

Project page: <https://thereprocase.github.io/projects/trio/>
