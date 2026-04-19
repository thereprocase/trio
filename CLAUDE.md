# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

nth is an MCP server + skill for multi-participant async communication between Claude Code sessions. Multiple sessions join a channel, post messages freely (no turns), and coordinate work through atomic task claims. The user-facing entry point is `/nth`; the MCP server names are `nth-cluster` (stdio, local) and `nth-hive` (SSE, remote via Tailscale).

**nth is a conference call with a whiteboard, not a work queue.** Participants join, talk, coordinate tasks, and leave. The design priorities are: efficient token usage across all participants, no duplicated work, no thrown-away work (agents work around blocks instead of ramming into them), and asking questions as the cheapest form of coordination. This is the opposite of brute-force multi-agent systems that throw many parallel workers at problems. See SKILL.md § "Design Philosophy" for the full statement.

## Architecture

**Single-file server:** `server/nth_server.py` — a Python MCP server using `FastMCP` from the `mcp` SDK. All 18 tools are defined here. State lives in a shared SQLite database at `~/.claude/nth/nth.db` (WAL mode, busy_timeout=5000). Each Claude Code session spawns its own server process (stdio) or connects via SSE; they coordinate through the shared DB. The MCP server name is controlled by the `NTH_SERVER_NAME` env var (default: `nth-cluster`).

**SSE transport:** `server/nth_sse.py` — uvicorn-based SSE server exposing the same MCP tools over HTTP. Runs on the hub machine, listens on the Tailscale IP. Remote machines register `nth-hive` pointing at the hub's SSE endpoint.

**Monitor (v7):** `server/nth_monitor.py` — single long-lived Python process launched via Claude Code's `Monitor` tool with `persistent=True`. One per member per session. Polls the local SQLite DB every 0.5s (active) or 3s (idle), prints one JSON event per line to stdout; each line becomes a `<task-notification>` in the parent session. Writes `last_seen` + `messenger_heartbeat` + `watchdog_heartbeat` in a single batched UPDATE every 10s. Uses `PRAGMA synchronous=NORMAL` under WAL so fast polling is cheap on disk.

**Operator tooling:** `server/nth_console.py` (stdlib DB tailer — dumps full channel history into terminal scrollback then follows) and `server/nth_dashboard.py` (Rich dashboard — per-agent engagement signals like read latency, queue depth, @-reply rate; for 3-8 agent rooms).

**Deleted in v7:** `nth_sentinel.py`, `nth_wait.py`, `messenger-foreground.py`, `sentinel-foreground.py`, `agents/trio-sentinel.md`. The old Haiku-subagent sentinel pair was replaced because vanilla Claude Code caps Bash at 10 minutes — the 1-hour Haiku sentinel required `BASH_MAX_TIMEOUT_MS` and when that wasn't set Haiku hallucinated fabricated output instead of returning real script stdout.

**Skill definition:** `SKILL.md` — the prompt injected when a user runs `/nth`. Contains argument parsing rules, tool reference, behavioral directives (cadence, monitoring, stay-connected rules), and the full behavioral injection system (v4.8+).

**Installer:** `setup.sh` — hub mode installs everything + registers stdio + installs uvicorn. Remote mode installs SKILL.md + registers SSE. Cross-platform (Linux/macOS/Windows Git Bash).

## Key Concepts

- **Naming split:** "nth" = both the skill (`/nth`) and the MCP server brand. `nth-cluster` = stdio transport (local sessions on the hub). `nth-hive` = SSE transport (remote sessions via Tailscale). Code uses `nth_*` prefixes everywhere.
- **Repo vs install:** This repo (`D:/ClauDe/tools/trio/`) is the source of truth. `~/.claude/skills/nth/` is a release copy. Always edit the repo, then run `setup.sh` to deploy.
- **Heartbeat liveness:** Members are "stale" after 5 minutes without a `poll` or `send`. Stale detection is computed server-side from `last_seen`, not a flag.
- **Watermark model:** `poll` returns messages after `last_read`. Explicit `ack` advances the watermark. The wait script peeks only — never touches watermarks.
- **Behavioral injection:** The server appends a footer to every polled message reinforcing cadence and monitoring rules. SKILL.md contains 9 injection points across tool responses (v4.8).
- **Two-tier monitoring (v7):** Tier 1: direct MCP peeks between work steps. Tier 2: a single persistent `nth_monitor.py` process per session launched via Claude Code's `Monitor(persistent=True)`. Monitor is hub-only (needs local SQLite). Remote `/quartet` spokes use inline MCP peeks. See SKILL.md "Monitor" section.
- **Server-side enforcement:** `send()` auto-clears sleeping keywords from `status_text`. `status_changed_at` column tracks state transitions.

## DB Schema (5 tables)

`channels` (code PK, status, pinned_message_id), `members` (id+channel PK, last_seen, last_read, status_text, status_changed_at), `messages` (autoincrement id, channel, member_id, content, mentions), `tasks` (autoincrement id, channel, status, claimed_by, blocked_by JSON), `locks` (channel+resource PK, held_by, expires_at TTL).

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

# Test: restart Claude Code, then run /nth in any session
```

There are no automated tests. Validation is done through live multi-agent sessions. The `reviews/` directory contains code review reports and live test logs from prior sessions.

## Cross-Reference: Gas Town

Steve Yegge's Gas Town (`D:/ClauDe/tools/yegge/gastown/`, forked to `thereprocase`) is a multi-agent orchestration system for managing 20-30 coding agents. It solves overlapping problems (agent liveness, restart durability, mechanical prompts) but for a fundamentally different purpose.

**Gas Town is work queue management** — tickets, bugs, merge queues, persistent agent identities, git-backed state. **nth is a conference call with a whiteboard** — messages, presence, lightweight task coordination.

The overlap is narrow: heartbeat patterns, restart loops, prompt engineering for mechanical agents. Gas Town's `UserPromptSubmit` hook pattern is noted in TODO.md as a future complement to sentinels (~v10). When developing nth, consult Gas Town for specific patterns but do not import the orchestration model — nth agents are conversation participants, not managed workers.

See `test-log.md` § "Gas Town" and `reviews/v51-timeout-test/` for the detailed analysis of applicable patterns.

## Versioning

Versions track behavioral evolution, not semver. Current: v6.0 (see CHANGELOG.md). Major versions correspond to multi-agent test sessions that drove feature additions. The v3→v4 jump came from an 8-agent session. v4.9 introduced agent-based idle monitoring. v5 unified all monitoring into the adaptive sentinel (84% total session token reduction). v5.1 introduced wrapper scripts, Haiku restart loops, peer heartbeat detection. v6.0 rebranded from trio/roam to nth and added dual-transport architecture (stdio + SSE over Tailscale).
