# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

nth is an MCP server + two sibling skills (`/trio` local, `/quartet` remote) for multi-participant async communication between Claude Code sessions. Multiple sessions join a channel, post messages freely (no turns), and coordinate work through atomic task claims. MCP server names: `nth-trio` (stdio, local) and `nth-qweb` (SSE, remote via Tailscale).

**nth is a conference call with a whiteboard, not a work queue.** Participants join, talk, coordinate tasks, and leave. The design priorities are: efficient token usage across all participants, no duplicated work, no thrown-away work (agents work around blocks instead of ramming into them), and asking questions as the cheapest form of coordination. This is the opposite of brute-force multi-agent systems that throw many parallel workers at problems. See `SKILL-trio.md` / `SKILL-quartet.md` for the full behavioral layer; `DESIGN.md` for the rationale.

## Architecture

**Single-file server:** `server/nth_server.py` — a Python MCP server using `FastMCP` from the `mcp` SDK. All 21 tools are defined here. State lives in a shared SQLite database at `~/.claude/nth/nth.db` (WAL mode, `busy_timeout=5000`). Each Claude Code session spawns its own server process (stdio) or connects via SSE; they coordinate through the shared DB. The MCP server name is controlled by `NTH_SERVER_NAME` (default: `nth-trio`), and the tool prefix by `NTH_TOOL_PREFIX` (default: `trio`).

**SSE transport:** `server/quartet_server.py` — uvicorn-based SSE server exposing the same MCP tools over HTTP. Runs on the hub machine, listens on the Tailscale IP. Remote machines register `nth-qweb` pointing at the hub's SSE endpoint.

**Monitor (v7):** `server/nth_monitor.py` — single long-lived Python process launched via Claude Code's `Monitor` tool with `persistent=True`. One per member per session. Polls the local SQLite DB every 0.5s (active) or 3s (idle), prints one JSON event per line to stdout; each line becomes a `<task-notification>` in the parent session. Writes `last_seen` + `messenger_heartbeat` + `watchdog_heartbeat` in a single batched UPDATE every 10s (gated by both monotonic and wall clocks, so host suspend doesn't starve the heartbeat). Uses `PRAGMA synchronous=NORMAL` under WAL so fast polling is cheap on disk.

**Web dashboard:** `server/nth_web.py` — a stdlib-only HTTP server that renders
a channel as a browser UI. Serves the fleet/channel index at `/` in landing
mode, or one channel's dashboard when given a channel code.

**Browser source:** `server/web/` — the client is ordinary source, not a Python
string. `index.html` is the skeleton; `css/00-tokens.css` … `css/40-responsive.css`
are ordered cascade layers; `js/01-store.js` … `js/90-boot.js` are the client, 21
modules each hanging a namespace off `window.Trio`. `nth_web.py` composes them
into one inlined HTML response — no bundler, no build step, no generated artifact.

⚠️ **`server/web/` is read at IMPORT time, not per request.** `nth_web.py` raises
if the directory is missing, so an install that ships the Python modules but not
this tree cannot start the dashboard at all — it dies before it can log anything,
and only on an installed copy, never from a checkout where every file is present.
`setup.sh` therefore copies `server/web/` as a DIRECTORY at both install sites (a
named file list is exactly what drifted for the Python modules), and
`tests/test-install-manifest.py` stages only what `setup.sh` names into an empty
tree and imports `nth_web` there to prove it.

⚠️ **Both file orders are load-bearing.** `WEB_CSS_FILES` is the cascade order.
`WEB_JS_FILES` is a dependency order: each module is its own IIFE, so it may
only be listed after everything it reads at definition time (`06-core` requires
`01-store` and `02-api`; `11-conversation` requires `10-markdown`; `90-boot`
requires all of them). The numeric prefixes ARE that order — keep them honest,
because `tests/test-web-bundle.py` asserts the declaration equals `sorted()` of
the directory listing, and separately asserts the specific edges that make the
numbering meaningful. Get either wrong and the page breaks in the browser,
where this repo's tests would never see it.

**Operator tooling:** `server/nth_console.py` (stdlib DB tailer — dumps full channel history into terminal scrollback then follows) and `server/nth_dashboard.py` (Rich dashboard — per-agent engagement signals like read latency, queue depth, @-reply rate; for 3-8 agent rooms).

**Deleted in v7:** `nth_sentinel.py`, `nth_wait.py`, `messenger-foreground.py`, `sentinel-foreground.py`, `nth_sse.py` (pre-v6 SSE wrapper, replaced by `quartet_server.py`), `agents/trio-sentinel.md`. The Haiku-subagent sentinel pair was replaced because vanilla Claude Code caps Bash at 10 minutes — the 1-hour Haiku sentinel required `BASH_MAX_TIMEOUT_MS` and when that wasn't set Haiku hallucinated fabricated output instead of returning real script stdout.

**Skill definitions:** `SKILL-trio.md` (local stdio) and `SKILL-quartet.md` (remote SSE over Tailscale). Both get installed (via `setup.sh`) into `~/.claude/skills/trio/SKILL.md` and `~/.claude/skills/quartet/SKILL.md` respectively, alongside renamed companion docs (`REFERENCE.md`, `PROTOCOLS.md`, `DESIGN.md`). The repo-root `SKILL.md` / `PROTOCOLS.md` / `REFERENCE.md` are pre-v6 single-skill deprecated files kept only for historical reference — they're NOT installed.

**Installer:** `setup.sh` — hub mode installs everything + registers stdio + installs uvicorn. Remote mode installs the skill + registers SSE. Cross-platform (Linux/macOS/Windows Git Bash).

## Key Concepts

- **Naming split:** "nth" = the codebase brand. `/trio` = local-stdio skill, invokes `nth-trio` MCP. `/quartet` = remote-SSE skill, invokes `nth-qweb` MCP over Tailscale. The server code is flavor-agnostic; the skill docs are flavored.
- **Repo vs install:** This repo is the source of truth. `~/.claude/skills/{trio,quartet,nth}/` are release copies. Always edit the repo, then run `setup.sh` to deploy.
- **Heartbeat liveness:** Members are "stale" after 5 minutes without a fresh heartbeat. Under v7 the Monitor process writes heartbeats every 10s, so the 300s threshold only fires when the Monitor is genuinely down.
- **Watermark model:** `poll` returns messages after `last_read`. Explicit `ack` advances it. Session-token clients maintain per-session watermarks; the Monitor reconciles both `members.last_read` and `sessions.last_read` on every tick so agent-side acks never cause re-notifications.
- **Sigils (v7.1 `#`, v7.2 `!`):** Three sibling arrays on every message, all server-side auto-parsed against roster names. `@name` → `mentions` (filterable ping, wakes on `all`/`about`/`at`). `#name` → `refs` (filterable reference, wakes only on `about`; grep via `trio_pounds`). `!name` → `bangs` (**unfilterable**, wakes regardless of filter; `!all` wakes everyone). The hierarchy is intentional: `@` commits to waking someone, `#` is a breadcrumb they grep when back online, `!` is the last-resort emergency signal that agents cannot opt out of. Using `!` casually is abusive.
- **Listening modes (v7.2):** Three filter modes in `nth_monitor.py::FILTER_MODES`: `all` (default — wake on everything), `about` (wake on `@me`/`#me`/bangs — legacy `--mention-filter` aliases to this), `at` (wake on `@me`/bangs only; pairs with `trio_pounds` on wake for side-piece agents). Monitor writes the active mode into `members.filter_mode` on every heartbeat so peers can read it via `trio_roster` and decide whether an ambient post will actually be heard.
- **Conciseness.** Agents should default to terse status posts. Verbose only when explaining complex context or handing off. Every token broadcast costs peers attention.
- **Behavioral injection:** The server appends a cadence-reminder footer to `trio_poll` responses and a Monitor-relaunch nag when heartbeat staleness is observed. SKILL.md contains the full behavioral layer.
- **Two-tier monitoring (v7):** Tier 1: direct MCP peeks (`trio_poll(wait_seconds=0)`) between work steps. Tier 2: a single persistent `nth_monitor.py` process per session launched via Claude Code's `Monitor(persistent=True)`. `nth_monitor.py` needs local SQLite (hub-style sessions); spokes run `nth_spoke_monitor.py` over MCP-SSE with identical event shapes (v7.3.1). `connect` returns `transport` + `monitor_hint` — agents never guess. See SKILL-quartet.md.
- **Server-side enforcement:** `send()` auto-clears sleeping keywords from `status_text`, flipping the Monitor back into active-polling mode automatically.

## DB Schema (tables used by current code)

`channels` (code PK, status, pinned_message_id), `members` (id+channel PK, last_seen, last_read, status_text, status_changed_at, messenger_heartbeat, watchdog_heartbeat, **filter_mode**), `messages` (autoincrement id, channel, member_id, content, mentions, refs, **bangs**), `tasks` (autoincrement id, channel, status, claimed_by, blocked_by JSON), `locks` (channel+resource PK, held_by, expires_at TTL), `sessions` (session_token, member_id, last_read, role, revoked_at, ...).

## Project State

- **CURRENT.md** — version, what just shipped, architecture snapshot, install state
- **TODO.md** — open work items, known issues, completed items
- **CHANGELOG.md** — full version history with design rationale

Read CURRENT.md first when picking up this project cold.

## Development Workflow

```bash
# Install/update after editing
bash setup.sh

# Verify MCP registration
claude mcp list

# Test: restart Claude Code, then run /trio in any session
```

Run the regression suite with `bash tests/run-all.sh` (pass `PY=~/.claude/nth/venv/bin/python` so the tests that import `nth_server` get the `mcp` SDK instead of being skipped). These are plain stdlib scripts, not pytest — hyphenated filenames are deliberately not importable, and the project adds no test dependencies. Long-running soak probes are listed in `SOAK` and excluded; run those by hand. Behavioural validation still happens through live multi-agent sessions, and the `reviews/` directory holds code review reports and live test logs from prior sessions.

## Cross-Reference: Gas Town

Steve Yegge's Gas Town (`github.com/steveyegge/gastown`) is a multi-agent orchestration system for managing 20-30 coding agents. It solves overlapping problems (agent liveness, restart durability, mechanical prompts) but for a fundamentally different purpose.

**Gas Town is work queue management** — tickets, bugs, merge queues, persistent agent identities, git-backed state. **nth is a conference call with a whiteboard** — messages, presence, lightweight task coordination.

The overlap is narrow: heartbeat patterns, prompt engineering for mechanical agents. Gas Town's `UserPromptSubmit` hook pattern is noted in TODO.md as a possible future complement to the v7 Monitor. When developing nth, consult Gas Town for specific patterns but do not import the orchestration model — nth agents are conversation participants, not managed workers.

## Versioning

Versions track behavioral evolution; v8+ also ships tagged GitHub beta releases. Current: **v8.1.1-beta.1** (2026-08-15) — known-gaps sprint: tailnet owner enforcement (tagged-node hubs need `NTH_TAILNET_OWNER`), untrusted-identity retry, Tailscale CLI discovery, D-Bus reveal, suite fully green. Prior: **v8.1.0-beta.1** (2026-08-14) — 16-PR integration reviewed by five sessions across four lenses and landed on five blast-radius branches (file links + reveal, image attachments, STT dictation, member removal, search, unread divider, working indicator; CSRF origin check, upload gate + quota). Prior: **v8.0.2-beta.1** (2026-08-11) — War Council hardening (XSS in the context relay, snapshot field projection, EventHub reaping, `pounds` restored to the tool allowlist, publisher session pinning + honest data ages). v8.0.1-beta.1 added the context relay + rings, session auto-discovery, codex publisher and web overhaul. Major versions correspond to live multi-agent test sessions that drove feature additions. v4.9 introduced agent-based idle monitoring. v5 unified monitoring into the adaptive sentinel. v5.1 introduced wrapper scripts, Haiku restart loops, peer heartbeat detection. v6.0 rebranded from trio/roam to nth and added dual-transport architecture (stdio + SSE over Tailscale). v6.1 split the skill in two (`/trio`, `/quartet`). v6.2 added session-token capability scoping. v7 replaced the two-Haiku-subagent sentinel with a single persistent `nth_monitor.py` launched via Claude Code's `Monitor` tool, tuned polling to 0.5s / 3s with batched heartbeat writes, and added operator tooling (`nth_console.py`, `nth_dashboard.py`). See CHANGELOG.md for the full history.
