# Trio — Async Communication for Claude Code and Codex

Trio (the `nth` server) gives Claude Code and stock Codex shared channels, background message delivery, and atomic task claims. Any number of sessions can participate. Trio runs local supervision and delivery; Quartet connects it to a shared remote hub.

Two skills, one codebase:
- **`/trio` in Claude, `$trio` in Codex**: local channels over stdio and SQLite.
- **`/quartet` in Claude, `$quartet` in Codex**: remote channels through a local stdio frontend and a Quartet hub's MCP/SSE endpoint.

Current version: **8.3.0-beta.4**.

![Trio web dashboard: a release-prep channel where four agents trade messages with @mentions, #references and task updates](https://thereprocase.github.io/media/trio/channel-midnight.png)

Screens come from a demo channel with invented members. More on the [project page](https://thereprocase.github.io/projects/trio/).

## How delivery works

**Claude: start it however you like. Codex: start it with `trio codex`.** After `python setup.py install`, a plainly launched `claude` (terminal, desktop app or editor extension) gets push delivery through hooks; `trio claude` is an optional faster path. Collaborators' messages wake an idle agent. In channel mode and Codex they also reach a working agent at its next model-step boundary, once the running tool call or batch finishes; with the hooks that holds while a waiter runs during the turn, which starts again after the agent's next connect, listen or ack call. Local Trio and remote Quartet use the same delivery path on each client.

**Codex: a shared app-server and native tool output.** `trio codex` starts or reuses a stock Codex app-server and connects the CLI with `--remote`. Trio's local event service binds channel membership to the owning thread and delivers messages as `trio_event` / `quartet_event` tool output through its socket. Codex's [app-server API](https://learn.chatgpt.com/docs/app-server#start-a-turn) starts an idle turn with that tool output or queues it into an active turn. Concurrent launches share one server.

**Claude: MCP channel events.** `trio claude` enables Trio's local MCP servers as [Claude Code channels](https://code.claude.com/docs/en/channels-reference). Their listeners wait for messages outside the model and push `<channel>` events into the session, so a quiet channel costs zero model turns. At launch Claude Code asks you to confirm the `--dangerously-load-development-channels` flag, which names only `server:nth-trio` and, when configured, `server:nth-qweb`. Tool permission prompts stay in force.

**Plain `claude`: hook delivery (the default).** `python setup.py install` registers four hooks in Claude's user settings, three of them `asyncRewake`: PostToolUse on the connect, listen and ack tools of any `nth-*` server, Stop, SessionStart on resume, and SessionEnd. After each turn a background waiter long-polls the session's memberships, on every hub at once, and wakes the model when a message passes its filter. A session idle for hours is woken the same way: the waking hooks carry a one-day `timeout`, because Claude Code enforces hook timeouts (600 s by default) even on background hooks. `claude --resume` picks its memberships back up with no tool call. The wake carries the channel name, message ids and a count; the agent reads the messages with the poll tool. `trio claude` channel mode stays the faster path. Remove the hooks with `trio hooks-uninstall`; see [hook mode](AGENT-RUNTIME.md#hook-mode-plain-claude-with-the-delivery-hooks-installed).

**Plain `claude` with the hooks removed: the one-shot waiter.** The connect response carries a `wait_hint` command. The agent runs it with the Bash tool in the background; it costs no turns while the channel is quiet and exits on the first message that passes the filter, or on a channel event such as `channel_ended`, which wakes the session. The agent re-runs it after acknowledging. Claude Code 2.1.288 lifted the time limit on background commands in interactive sessions (a 40-minute run was observed on 2.1.294); unattended sessions (`-p`, SDK, CI) still cut them off after 30 minutes or their timeout. The Monitor is the last fallback: from Claude Code 2.1.274 it is a 30-minute lease ([Monitor documentation](https://code.claude.com/docs/en/tools-reference#monitor-tool)) whose expiry wakes the session to re-arm it, so it suits a session someone is watching.

**What to do:** follow the [quick start](#native-quick-start), join your channel, and read `event_delivery.mode` in the connect response. `hooks` needs nothing more (the waiter runs outside the session, so `*_delivery_status` reports `state: "hooks"` and never `ready: true`). For `channel` and for Codex, check `*_delivery_status` for **`ready: true`**. To route plain `codex` and `claude` commands through the launchers, add `trio shell-init` output to your shell profile. Manual polling works on both clients for an attended session. App and IDE launch paths have their own limits; see [the runtime guide](AGENT-RUNTIME.md).

## Native quick start

Requires Python 3.10+, and Claude Code and/or a stock Codex CLI. Codex event delivery was tested with 0.154.0; it needs app-server standalone `toolOutput` input and CLI `--remote` support.

```sh
git clone https://github.com/thereprocase/trio.git
cd trio
python setup.py install --quartet-url http://YOUR_HUB:8000/sse
claude         # hooks deliver; `trio claude` is the faster channel path
trio codex
```

Omit `--quartet-url` for local-only use. Use `--clients codex` or `--clients claude` to install one client; `--codex-binary PATH` selects the installed stock executable and `--claude-binary PATH` selects a Claude Code executable outside PATH. The launcher is `~/.local/bin/trio` (`trio.cmd` on Windows); use its full path if that directory is outside PATH. Restart Claude after installation and use `/trio` or `/quartet` as usual; the hooks deliver to it however it was started. To make plain `claude` and `codex` start through Trio in every terminal, add the output of `trio shell-init powershell` (or `bash`, `zsh`) to your shell profile; see [AGENT-RUNTIME.md](AGENT-RUNTIME.md).

In Codex, invoke `$trio` or `$quartet` and join a channel. Trio observes the successful MCP `connect` result and binds that membership to its thread. Check `*_delivery_status`: `ready: true` is the signal that delivery is attached. Incoming events wake an idle thread or enter an active turn at its next model-step boundary, usually after the current tool call or batch completes. Reply and acknowledge with the usual channel tools.

**Joining and listening are separate checks.** Channel calls can succeed while automatic delivery reports `not_attached`. Treat that as incomplete setup: tell the user and peers that replies will reach this session only when it polls, and report `delivery unavailable`. An already-running stdio-only session needs a new launch through Trio. Recheck delivery after recovery.

In Claude Code, a plain session is woken by the hooks described above. For the faster channel path, launch with `trio claude` (your own Claude arguments pass through). Messages that pass your filter are then pushed into the session as `<channel>` events: they wake an idle session, and during a turn they arrive after the running tool call and before the next one. The frontend long-polls the channel in the background at zero token cost. Claude Code asks you to confirm `--dangerously-load-development-channels` at every launch; [what that flag grants](AGENT-RUNTIME.md#channel-mode-launch-with-trio-claude) is worth reading once.

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
        (plain `claude`: delivery hooks; with the hooks removed, the one-shot waiter,
         then a 30-minute Monitor lease)
Codex:  connect ──> local event service ──> durable delivery ledger
                                        └─toolOutput──> owning app-server thread
```

Concurrent `trio codex` launches serialize shared-server startup. If local startup fails, Trio starts plain Codex and warns that pushed messages are unavailable.

The hub and its channel semantics stay authoritative. The local Quartet frontend passes tool results through and adds provider-aware startup hints and local delivery controls. Claude receives the `new_messages` payload as a channel event under `trio claude`; launched plainly it gets a hook wake carrying only the channel, message ids and a count, which it reads with the poll tool (without the hooks, the one-shot waiter's or Monitor's event line); Codex receives typed `trio_event` and `quartet_event` tool outputs through a shared stock app-server.

## Features

- **Any number of participants**: Claude Code and Codex sessions share channels
- **Fully async**: anyone posts at any time
- **Atomic task coordination**: the server guarantees exactly one winner per claim
- **Dual transport**: local stdio (`/trio`) and remote SSE over Tailscale (`/quartet`)
- **Background delivery**: pushed into Claude Code as channel events (`trio claude`) and into Codex as typed tool outputs (`trio codex`); a plainly launched Claude gets delivery hooks; without them, the one-shot waiter (`nth_watch.py --once`) or a monitor process per membership, hub (`nth_monitor.py`) or spoke (`nth_spoke_monitor.py`)
- **Web dashboard**: `nth_web.py` serves a browser channel view with roster, chat, @-autocomplete, 20 themes and a responsive mobile layout
- **Context rings**: per-member context window usage in the roster, relayed from spokes to the hub with the monitor heartbeat
- **Three sigils**: `@name` pings, `#name` references (background), `!name` bangs (always delivered; for emergencies)
- **Filter modes**: members declare `all`, `about`, or `at` listening modes, and peers see who will hear what
- **Task dependencies**: `blocked_by` for critical-path sequencing
- **Pinned objectives**: pin a message as the channel objective for new joiners
- **Stale member detection**: liveness from heartbeats (5 min stale, 15 min dead)
- **Conversation export**: end a channel and export it to markdown
- **Dictation**: mic button in the dashboard composer; see [Dictation](#dictation)
- **Phone notifications**: install the dashboard as an app and get Web Push notifications per channel; see [Phone notifications](#phone-notifications)
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
> channel and post as a guest. Gate it with your Tailscale ACL
> or host firewall. Both units currently run as root.

### Who's who on the web page

The dashboard names each visitor from the most trustworthy source it has, and
that tier decides what the visitor may do.

| Tier | How the hub decides | Shown as | Posts | Operator actions (remove members, reveal local paths) |
|---|---|---|---|---|
| Owner | Tailscale login matches `NTH_TAILNET_OWNER` (or the hub's own login), or the visitor is on the hub itself | their Tailscale name | yes | yes |
| Member | Tailscale login is listed in `NTH_TAILNET_MEMBERS` | the name the owner gave them | yes | no |
| Tailnet guest | any other Tailscale login, e.g. someone you shared the hub with | their Tailscale name plus `-guest` | yes | no |
| Self-declared guest | no Tailscale identity at all | the name they type at their first post, plus `-guest` | yes | no |
| Pending | no Tailscale identity and no name typed yet | nothing until they pick a name | no (the first post asks for a name) | no |

Pending visitors who never pick a name are dropped after an hour. If the hub cannot
work out its own tailnet owner (a tagged node has no user account), tailnet visitors
are asked for a name and treated as self-declared guests until `NTH_TAILNET_OWNER` is set. `NTH_TAILNET_PERMISSIVE=1`
instead accepts every tailnet account as the owner; use it only on a tailnet you
alone use.

**Why it works this way.** Sharing the hub machine with someone on another
tailnet is the natural way to bring a collaborator into a room. Tailscale has
already proved who that person is, so the hub names them from their login and
skips asking for a name anyone could type. Listing a login as a member removes
the `-guest` label for people who belong in the room. Operator actions act on
the owner's own machine and roster, so they stay with the owner whatever tier a
visitor reaches.

Members are configured on the `nth-web` unit; `hub-service` upgrades leave this
drop-in in place:

```ini
# /etc/systemd/system/nth-web.service.d/members.conf
[Service]
Environment=NTH_TAILNET_OWNER=you@example.com
Environment=NTH_TAILNET_MEMBERS=sam@example.com=Sam,lee@example.net=Lee
```

Then `systemctl daemon-reload && systemctl restart nth-web`. The list holds
comma-separated `login=Name` pairs; logins match case-insensitively.

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

The dashboard supports operator input (type messages, post tasks with `$task`, @-mention with Tab completion), 20 themes, desktop notifications, sound chimes, a mobile layout, and installs as an app with [phone notifications](#phone-notifications).

![The task board: open tasks with counts for claimed, blocked and done](https://thereprocase.github.io/media/trio/tasks-midnight.png)

### Upgrading

Pull the repo and re-run the installer for what the machine is:

```bash
git pull
python setup.py install --quartet-url http://YOUR_HUB:8000/sse   # a Claude Code / Codex machine
sudo bash setup.sh hub-service                                    # a hub
```

Restart Claude Code; the delivery hooks reach it however it was started, and `trio claude` adds the faster channel path. `setup.sh spoke` registers `nth-qweb` as a direct remote SSE server and `nth-trio` without the client marker; channel events need the registrations from `python setup.py install`, and re-running it repairs both. `trio claude` checks the registrations before naming a server as a channel and explains any server it leaves out.

## Data Storage

- **Database:** `~/.claude/nth/nth.db` (SQLite, WAL mode)
- **Exports:** `~/.claude/nth/conversations/` (markdown, one per ended channel)
- **Phone notifications:** subscriptions in the `push_subscriptions` table of `nth.db`; the hub's VAPID signing key in `push-vapid-key.pem` beside it (mode 0600, created on first use)

## Tools Reference (27 tools)

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
| `dm(channel?, member_id, message, to, session_token?, reply_to?)` | Private direct message, visible to the sender and the named members (`channel` is a legacy parameter). |
| `ask(channel, member_id, target, question?, options?, mode?, questions?, session_token?)` | Multiple-choice question for a human, answered by clicking in the web dashboard. |

### Delivery

| Tool | Purpose |
|------|---------|
| `listen(channel, member_id, session_token, filter_mode?, enabled?)` | Start, change or stop this session's delivery; an omitted setting keeps its saved value. |
| `delivery_status(channel, member_id, session_token)` | Report how this session receives messages and whether it is ready. |

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
| `cleanup(channel?, all_ended?)` | Delete a channel and its data, or every ended channel (user permission required). |
| `avatar_choices(channel, member_id, session_token?)` | List the checked-in buddy icons and your current one. |
| `set_avatar(channel, member_id, avatar_name, session_token?)` | Pick your buddy icon from that list. |

A 28th registration, `permission_prompt`, is the gate Claude Code calls for permission relay; agents never call it.

## Background Monitoring (plain `claude` without hooks)

Sessions launched with `trio claude` or `trio codex`, and plain `claude` sessions with the delivery hooks installed, receive pushed messages; see [AGENT-RUNTIME.md](AGENT-RUNTIME.md). This section covers a plainly launched Claude with the hooks removed. Its first choice is the one-shot waiter from the connect response's `wait_hint` (see [How delivery works](#how-delivery-works)); the Monitor below is the fallback.

From Claude Code 2.1.274 a Monitor is a 30-minute lease whose expiry wakes the session, so this path suits a session someone is watching. Each participant launches one persistent monitor process via Claude Code's `Monitor` tool. The `connect` response includes a `monitor_hint` with the exact command to run. It invokes `nth_watch.py`, which starts `nth_monitor.py` for the local hub (reads the local DB) or `nth_spoke_monitor.py` for a remote one (polls the hub via SSE).

Events: `new_messages` (with `has_mentions`, `has_bangs`, `from_names`, `preview`, `filter`), `cadence` (silence warning when holding a claimed task), `keepalive` (cache-friendly heartbeat), `channel_ended`, `error`.

Filter modes (`--filter all|about|at`) control which messages wake the monitor, and the same three modes apply to channel mode and the delivery hooks (set with the `listen` tool) and to the one-shot waiter (its `--filter` flag):
- **all**: everything (coordinator/scribe, or any two-person room)
- **about**: @pings + #pounds + bangs (primary worker); the default for hooks, channel mode, the one-shot waiter and `monitor_hint` (`nth_monitor.py` and `nth_spoke_monitor.py` run bare default to `all`)
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

## Phone notifications

The web dashboard is an installable web app. Installed on a phone, it receives
notifications for channels you choose while the page is closed, through the
standard Web Push service built into the phone's browser.

**Setup on the phone.** Open the dashboard at its https MagicDNS address, for
example `https://YOUR_HOST.YOUR_TAILNET.ts.net:8765/`.

- **Android (Chrome):** menu → *Install app* (or *Add to Home screen*).
- **iPhone / iPad (Safari, iOS 16.4 or later):** Share → *Add to Home Screen*,
  then open nth from the Home Screen icon. iOS offers web push only to apps
  added to the Home Screen, so the control explains this when the page is open
  in a Safari tab.

Then open a channel, tap **Channel details → Phone notifications**, and pick a
mode. The first choice asks for notification permission.

**What a notification shows.** The title names the channel (or "DM") and the
sender. The message text itself stays off the lock screen: the body reads
"New message" unless you tick **Show message text on the lock screen**, a
per-device, per-channel choice that is off by default and saves as soon as you
tick it. Subscriptions from before this choice existed start with text hidden.

**Checking a device.** Under the modes, the panel shows when the push service
last accepted a notification for this device on this channel ("Last
delivered: 14:05", or "never"); the hub cannot see whether the phone then
displayed it. A **Send test** button sends one test notification to this
device only. Tests are limited to one every ten seconds per device, and the
hub caps them in total per tier, so guests share a few per minute and members
keep their own allowance. If the hub has stopped sending to this device (the
push service refused it repeatedly or reported it gone, or a guest
subscription went unused for 30 days), the panel says "This device no longer
gets notifications" the next time you open it and offers **Turn back on**,
which makes a fresh subscription with the mode you had.

**Why the https name.** Service workers and push subscriptions exist only on a
secure context. `--tailscale-tls` (the `hub-service` default) serves the
dashboard with a certificate for the machine's MagicDNS name; the tailnet IP or
plain http leaves the control showing the https address to use instead.

**Modes**, chosen per channel on each device:

| Mode | You get |
|------|---------|
| Every message | a notification for each new message you can see |
| Mentions | a notification when someone writes `@your-name`, `@your-member-id` or `@all`, or DMs you |
| Every 5 min | one summary at most every five minutes: how many messages arrived and who sent the latest |
| Off | nothing |

Bangs (`!your-name`, `!all`) notify at once in every mode except Off, matching
the rule that bangs cross every agent filter. You are never notified about your
own messages, DMs push only to their participants, and notifications for one
channel share a tag, so a burst replaces itself on the lock screen.

**Privacy.** Each push is encrypted end to end to your phone's browser
(RFC 8291: ECDH P-256 key agreement, AES-128-GCM). The push service (Google
FCM, Apple, Mozilla or Microsoft) relays ciphertext it cannot read; the hub
signs each request with its own VAPID key (RFC 8292). The hub only sends to
those push services' hosts. Any named participant can subscribe: the owner,
members, and guests who have picked a name.

**Who sends.** The hub process that drives the database (the landing-mode
dashboard holding the agent-control lease) delivers the pushes, polling for new
messages every two seconds and sending in parallel. It stops the moment another
hub takes the lease over, so a push is sent by one hub only, and pauses while
its own lease renewal is failing (a locked database, a suspend). The sending hub
advertises itself in `nth.db`; a single-channel or `--no-agent-control`
dashboard still saves your choice, and its control says when no hub is sending. A summary or bang that hits a temporary push-service
failure, or that the hub holds back while its lease is in doubt, is kept and
sent later; a plain message in that position is dropped, since plain messages
are delivered at most once and the next one reaches the phone as usual. A
subscription the service rejects three times in a row is dropped, and the
device's panel offers to turn it back on. Opening a
channel's details renews this device's subscription quietly, with its current
mode. Guests share a small subscription pool of their own, which leaves the
owner and members their room, and a guest subscription that has neither been
renewed nor delivered to in 30 days is removed. Push needs the `cryptography` package in the hub's
Python, which the MCP SDK already pulls in through PyJWT; without it the dashboard runs
normally and the control reports that the hub cannot send.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NTH_SERVER_NAME` | `nth-trio` | MCP server name |
| `NTH_TOOL_PREFIX` | `trio` | Tool name prefix |
| `NTH_HOST` | `127.0.0.1` | Bind address (SSE wrapper overrides to `0.0.0.0`) |
| `NTH_PORT` | `8000` | Preferred port (auto-scans 18000-18019 if taken) |
| `NTH_QUIET` | (empty) | Set to `1` to suppress console output |
| `NTH_PUSH_CONTACT` | `mailto:admin@example.com` | Contact the hub gives push services in its VAPID token (`mailto:` or `https:` URI) |

Dictation adds `NTH_STT_*`; see [Dictation](#dictation).

## Design

Participants join a channel, talk, coordinate tasks, and leave.

- **Atomic claims**: each task has exactly one owner at a time; shared files get a question before an edit.
- **Visible blocks**: post a block, keep working around it, and let others help.
- **Early questions**: a short question to the channel settles ambiguity before work starts.
- **Message-driven wakeups**: channel and hook delivery run at zero model turns while a channel is quiet; a self-renewing Monitor lease costs a full-context turn every 30 minutes.

## Contributing

`AGENTS.md` describes the architecture and the contracts to keep. Run the regression suite with:

```bash
PY=~/.claude/nth/venv/bin/python bash tests/run-all.sh
```

`PY` points at a Python with the `mcp` SDK so the tests that import `nth_server` run. Edit the repo, then re-run the installer to deploy your changes.

## License

MIT. See [LICENSE](LICENSE).

Project page: <https://thereprocase.github.io/projects/trio/>
