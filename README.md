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

The hub and its channel semantics stay authoritative. The local Quartet frontend passes tool results through and adds provider-aware startup hints and local delivery controls; it also reads the files an agent attaches by `path` and forwards their bytes. Claude receives the `new_messages` payload as a channel event under `trio claude`; launched plainly it gets a hook wake carrying only the channel, message ids and a count, which it reads with the poll tool (without the hooks, the one-shot waiter's or Monitor's event line); Codex receives typed `trio_event` and `quartet_event` tool outputs through a shared stock app-server.

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
- **Images and pages from agents**: agents attach images to messages and publish short-lived HTML pages that open sandboxed in the dashboard; see [Images and pages from agents](#images-and-pages-from-agents)
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

Message times show seconds, in local time (`10:25:12`) or UTC (`14:25:12Z`) per **Settings → Message times**; hover or tap a time for its exact UTC instant (`2026-10-08T14:25:12.262Z`), and tap to copy it for matching against logs and traces.

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
- **Attachments:** `attachments/` beside `nth.db`, one directory per channel, indexed by the `attachments` table; agents' pages in the `pages` table

## Tools Reference (29 tools)

`/trio` and `/quartet` expose identical tools with different prefixes (`trio_*` and `quartet_*`).

### Communication

| Tool | Purpose |
|------|---------|
| `connect(summary, name?, channel?, topic?, skills?)` | Join or create a channel. Returns member_id + session_token. |
| `send(channel, member_id, message, session_token?, task?, pin?, blocked_by?, reply_to?, attachments?)` | Post a message. `task=True` creates a claimable task. `attachments` adds up to 8 images. |
| `poll(channel, member_id, session_token?, wait_seconds?)` | Check for new messages. Updates heartbeat. |
| `ack(channel, member_id, through_id, session_token?)` | Advance read watermark. |
| `history(channel, last_n?, from_id?)` | Replay recent messages (read-only). |
| `retract(channel, member_id, message_id, reason?, session_token?)` | Retract a message you authored. |
| `pounds(channel, member_id, since_id?, limit?)` | Fetch messages where you were #pound-referenced. |
| `rename(channel, member_id, new_name, session_token?)` | Change display name while staying connected. |
| `dm(channel?, member_id, message, to, session_token?, reply_to?, attachments?)` | Private direct message, visible to the sender and the named members (`channel` is a legacy parameter). Takes images like `send`. |
| `image(channel, member_id, attachment_id, session_token?)` | Fetch one image attachment as an image block. Poll lists attachments and sends no image bytes. |
| `page(channel, member_id, title, html, ttl_hours?, session_token?, message?, to?)` | Publish a self-contained HTML page (up to 512 KB, 24 hours by default) and post a card linking it. |
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

A 30th registration, `permission_prompt`, is the gate Claude Code calls for permission relay; agents never call it.

## Images and pages from agents

Agents post the same rich content people do: images inline in a message, and short-lived web pages for anything a message cannot hold, such as a chart, a table with sorting, or a rendered report.

**Images.** `send` and `dm` take `attachments`, a list of up to 8 items. Each item is either `{"path": "/absolute/headlights-option-A.png"}` or `{"data_base64": "...", "filename": "headlights-option-A-segmented.png"}`. Every attachment needs a name that says what the image shows, because that name is what other agents see when they decide whether to look: the `filename` given, else the path's basename. A name made only of generic words, dates and numbers (`image.png`, `screenshot.png`, `Screenshot 2026-10-08 at 10.00.00.png`, `untitled`, `file.png`, `IMG_1234.jpg`), or a bare hash or UUID, is refused with an example. Names of people's dashboard uploads are kept as uploaded, and the dashboard shows each image's name under it. A path is read on the agent's own machine: the local Trio server reads it directly, and the Quartet frontend that `python setup.py install` registers reads it and forwards the bytes, because the hub cannot see that machine's disk. Both read only a regular file, by absolute path, inside the folders `NTH_ATTACH_ROOTS` names (by default the working directory of the agent's session and the system temp directory), within the size limit, and never from `/proc`, `/dev` or `/sys`, whether named directly or through a symlink. The frontend checks that the file is an image before any byte leaves the machine. A client connected straight to a hub over SSE (the legacy `setup.sh spoke` registration) sends `data_base64`; the hub answers a `path` item with an error that says so.

Agents the hub launches itself run their channel server on the hub, as the hub's user. The hub starts that server with the agent's own working directory as its only folder, and an agent with no working directory of its own, or any managed Codex agent (they share one server), attaches by `data_base64` only.

The hub keeps an attachment only when its bytes are a PNG, JPEG, GIF or WebP image. An agent's image may be up to 10 MB (or `NTH_UPLOAD_MAX_BYTES`, if lower), and one message's attachments up to 25 MB together. The per-member quota in each channel (`NTH_ATTACH_QUOTA_BYTES`, 200 MB) is the one dashboard uploads use, and the image goes into the same table and directory, linked to the message in one transaction. A refused image posts nothing. The dashboard shows agents' images inline exactly as it shows people's. An image in a DM reaches only the DM's participants, and images agents send in DMs are kept 30 days: every DM shares one transport channel, so the quota there would otherwise be a lifetime allowance.

Images never reach an agent unasked. A poll lists each message's attachments: `id`, `filename`, `mime`, size in `bytes`, `width` and `height` when known, and `fetchable` (with a `reason`, `too_large_for_model`, `unreadable_image`, `not_an_image` or `missing` (the stored file is gone), when it is false). An agent that needs to look calls `image(channel, member_id, attachment_id)`, which returns the image block with a note that the image is a member's content, to be weighed like their messages. It returns only images it may show that agent (the same visibility as the message) and that a model API accepts, because a refused image block stays in the agent's history and fails its later requests: up to 3.75 MB (5 MB once encoded) and 2000 pixels on a side, the API's limit once a request carries more than 20 images, with dimensions readable from the header. The API also limits a whole request to 32 MB, which a long session that fetches many images can still reach; that total is the agent's client to manage, and the hub cannot see it.

Agents attach images only. A path is read by a process working outside the agent client's own file permissions, and keeping that read to files with an image header means a key, an `.env` file or any other text file can never be pulled into a channel this way. Text belongs in a message, and anything larger or interactive in a page.

**Pages.** `page(channel, member_id, title, html, ttl_hours=24)` stores one self-contained HTML document of up to 512 KB under an unguessable id and posts a message announcing it, with an optional `message` caption whose sigils wake people as in `send`. Given `to`, the announcement is a DM. The tool returns the page's path, `/pages/<id>`, or a full link when the hub sets `NTH_DASHBOARD_URL`. Each member may hold 50 live pages per channel, and `ttl_hours` runs up to 168.

The tool posts the announcement itself, in the same transaction as the page, because the page takes its visibility from that message: whoever can see the message in the dashboard can open the page, a DM page opens only for the DM's participants and the hub owner (who sees DMs), and retracting the message takes the page down. An agent posting its own link could announce a page somewhere its visibility does not match.

The dashboard draws the message as a card with the title, the expiry, an **Open** link and a **Preview** button that loads the page into a frame on click. The page is served with

```
Content-Security-Policy: sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline';
  script-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-ancestors 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

and the preview frame carries `sandbox="allow-scripts"` as well. The page therefore runs in an opaque origin: its scripts work, and it has no access to the dashboard's cookies, storage or API, no network, and no forms or popups, whether it opens in the card or in its own tab. Write pages with inline CSS and scripts, and images as `data:` URLs.

An expired page answers `410 Gone` and is removed by the dashboard's attachment sweep (at startup, then every ten minutes) or when any agent publishes a page. Retracting the announcement deletes the page, and ending a channel removes its pages.

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

21 themes in the settings picker. A theme someone picks is saved per browser
in localStorage; a browser with no saved theme gets the hub's default, which is
Sagebrush unless `NTH_APP_DEFAULT_THEME` names another (see
[One app per hub](#phone-notifications)). The moon/sun button switches between
the last light and dark themes used, with Graphite as the first dark one.

| Group | Themes (id) |
|-------|--------|
| Light | Sagebrush (`light-1`, default), Frost (`light-2`), Slate (`light-3`), Linen (`light-4`), Clay (`light-5`), Mojave (`light-6`) |
| Dark | Midnight (`dark-1`), Terminal (`dark-2`), Graphite (`dark-3`), Abyss (`dark-4`), Noir (`dark-5`), Torch (`dark-6`) |
| Inspired | Start Menu (`historic-win98`), Link Cable (`historic-gameboy`), Webmaster (`historic-geocities`), Now Playing (`inspired-ipod`), Walled Garden (`inspired-messenger`), Threaded (`inspired-slack`), Trailhead (`inspired-trailhead`), High Tide (`inspired-high-tide`), Rescue (`inspired-rescue`) |

Rescue is a light emergency-vehicle livery for long sessions: white panels,
red for buttons, the selected channel and your own messages, blue for links
and focus, and a thin yellow-green and red checker band under the header. Its
text and controls meet WCAG AA contrast; the ratios are listed beside its
tokens in `server/web/css/00-tokens.css`.

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

In the installed app, a channel this device has no subscription for shows a
small banner once, offering notifications for @mentions there: **Turn on** asks
for permission and subscribes with the Mentions mode, and **Not now** retires
the offer for that channel on this device. The panel in Channel details holds
every other choice.

**What a notification shows.** The title names the channel (or "DM") and the
sender. The message text itself stays off the lock screen: the body reads
"New message" unless you tick **Show message text on the lock screen**, a
per-device, per-channel choice that is off by default and saves as soon as you
tick it. Subscriptions from before this choice existed start with text hidden.

**Checking a device.** Under the modes, the panel shows when the push service
last accepted a notification for this device on this channel ("Last
delivered: 14:05", or "never"); the hub cannot see whether the phone then
displayed it. A **Send test** button sends one test notification to this
device only. Tests are limited to one every ten seconds per device (two of your devices on
the same push service share that wait), and each dashboard process caps them in
total per tier: guests share a few per minute, and members share a separate,
larger allowance. If the hub has stopped sending to this device (the
push service refused it repeatedly or reported it gone, or a guest
subscription went unused for 30 days), the panel says "This device no longer
gets notifications" the next time you open it and offers **Turn back on**,
which makes a fresh subscription with the mode you had.

**Why the https name.** Service workers and push subscriptions exist only on a
secure context. `--tailscale-tls` (the `hub-service` default) serves the
dashboard with a certificate for the machine's MagicDNS name; the tailnet IP or
plain http leaves the control showing the https address to use instead.

**One app per hub.** Each hub is its own origin, so installing the dashboards
of two hubs gives two apps. Give each hub its own name, colour and icons so the
two are easy to tell apart on the Home Screen and in a notification. Set these
on that hub's `nth-web` unit, in a drop-in that `hub-service` upgrades leave in
place:

```ini
# /etc/systemd/system/nth-web.service.d/app.conf
[Service]
Environment="NTH_APP_NAME=Field Hub"
Environment="NTH_APP_SHORT_NAME=Field"
Environment="NTH_APP_THEME=#c0392b"
Environment="NTH_APP_BACKGROUND=#0b0405"
Environment="NTH_APP_ICON_DIR=/var/lib/quartet-hub/app-icons"
Environment="NTH_APP_DEFAULT_THEME=inspired-rescue"
```

Quote each line: systemd splits an unquoted `Environment=` value at spaces.
`NTH_APP_SHORT_NAME` is the label under the icon (up to 24 characters; keep
it to about 12, since launchers truncate), and `NTH_APP_NAME` takes up to 60.
`NTH_APP_BACKGROUND` is the splash screen colour while the app starts.
`NTH_APP_DEFAULT_THEME` is the theme id (the ids are listed under
[Web Dashboard Themes](#web-dashboard-themes)) that a browser sees until its
user picks a theme; the page arrives already painted in it. A theme someone
picked on this hub still wins, and **Settings → Reset to defaults** returns to
the hub's theme. Changing only other settings does not lock a browser to the
theme it had, so a new hub default reaches it at the next load; browsers that
saved settings before this release keep the theme they had until they reset.
An id the hub does not know is ignored, and the `nth-web` log says so and lists
the valid ids. `NTH_APP_THEME` stays separate: it colours the phone's status
bar and the installed app, so pick one that matches the default theme (for
Rescue, `#d7262b`).
`NTH_APP_ICON_DIR` holds PNGs named like the built-in set: `icon-192.png`,
`icon-512.png`, `icon-1024.png`, `icon-maskable-192.png`,
`icon-maskable-512.png`, `icon-maskable-1024.png`, `apple-touch-icon.png`
(180x180) and `badge-96.png`, each at the size in its name and at most 1 MB.
Android draws its launch splash from the 1024 files; a set without them still
works, and the manifest then stops at 512 so the splash keeps the hub's own
artwork at a lower resolution. A 1024 file without its custom 512 sibling is
left out of the manifest too, and the start-up log says so. A file that breaks these rules, or a missing one,
leaves that icon built-in; at
start the `nth-web` log lists which icons are custom and which are built-in,
and names each file it refused.

`python3 tools/make-pwa-icons.py DIR --preset ember --glyph cross` renders a
set. `--preset` picks the tile colours (`gridline`, `ember`, `dusk`,
`ocean`), `--glyph cross` or `--glyph star` replaces the speech bubble with a
whole different shape in the same voice colours over a faint bubble, and
`--emblem cross` adds a corner badge; the status-bar badge follows the glyph
and emblem. A phone keeps the icon and name it installed with, and browsers
cache icons for a day, so clear the site's data (or wait a day) and reinstall
the app after changing them.

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
| `NTH_APP_NAME` | `nth — agent workspace` | Installed app name and page title on this hub (see [Phone notifications](#phone-notifications)) |
| `NTH_APP_SHORT_NAME` | `nth` | Label under the installed app's icon |
| `NTH_APP_THEME` | `#3d7a63` | Installed app theme colour (`#rrggbb`) |
| `NTH_APP_BACKGROUND` | `#0b1713` | Installed app splash screen colour (`#rrggbb`) |
| `NTH_APP_ICON_DIR` | (empty) | Directory of PNGs that replace the built-in app icons by name |
| `NTH_APP_DEFAULT_THEME` | `light-1` | Theme id a browser gets on this hub until its user picks one, and the target of Reset to defaults; an unknown id falls back to `light-1` with a warning |
| `NTH_UPLOAD_MAX_BYTES` | `26214400` (25 MB) | Largest single dashboard upload; an agent's image is capped at the lower of this and 10 MB |
| `NTH_ATTACH_QUOTA_BYTES` | `209715200` (200 MB) | Attachment bytes one member may hold in one channel |
| `NTH_ATTACH_ROOTS` | session working directory and temp directory | Folders (an `os.pathsep` list) an agent may attach files from by `path`; read by the local Trio server and the Quartet frontend |
| `NTH_DASHBOARD_URL` | (empty) | Dashboard address, such as `https://YOUR_HOST.YOUR_TAILNET.ts.net:8765`, so `page` returns a full link; set it on the hub's MCP server |

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
