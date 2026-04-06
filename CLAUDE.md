# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Trio is an MCP server + skill for multi-participant async communication between Claude Code sessions. Multiple sessions join a channel, post messages freely (no turns), and coordinate work through atomic task claims. The user-facing entry point is `/trio`; the MCP server name is `roam-hive-mind`.

## Architecture

**Single-file server:** `server/roam_hive_mind_server.py` — a Python MCP server using `FastMCP` from the `mcp` SDK. All 18 tools are defined here. State lives in a shared SQLite database at `~/.claude/roam/roam.db` (WAL mode, busy_timeout=5000). Each Claude Code session spawns its own server process; they coordinate through the shared DB.

**Background monitor:** `server/roam_hive_mind_wait.py` — standalone script that polls SQLite directly (not MCP) every 3 seconds for new messages. Designed to run via `run_in_background=true` with `timeout=600000`.

**Skill definition:** `SKILL.md` — the prompt injected when a user runs `/trio`. Contains argument parsing rules, tool reference, behavioral directives (cadence, monitoring, stay-connected rules), and the full behavioral injection system (v4.8+).

**Installer:** `setup.sh` — copies files to `~/.claude/skills/trio/`, registers MCP via `claude mcp add`, allowlists tools in `~/.claude/settings.json`. Cross-platform (Linux/macOS/Windows Git Bash).

## Key Concepts

- **Naming split:** "trio" = the skill (`/trio`). "roam" / "roam-hive-mind" = the MCP server and its tools. Code uses `roam_hive_mind_*` prefixes everywhere.
- **Repo vs install:** This repo (`D:/ClauDe/tools/trio/`) is the source of truth. `~/.claude/skills/trio/` is a release copy. Always edit the repo, then run `setup.sh` to deploy.
- **Heartbeat liveness:** Members are "stale" after 5 minutes without a `poll` or `send`. Stale detection is computed server-side from `last_seen`, not a flag.
- **Watermark model:** `poll` returns messages after `last_read`. Explicit `ack` advances the watermark. The wait script peeks only — never touches watermarks.
- **Behavioral injection:** The server appends a footer to every polled message reinforcing cadence and monitoring rules. SKILL.md contains 9 injection points across tool responses (v4.8).
- **Three-tier monitoring (v4.9):** Tier 1: direct MCP peeks between work steps. Tier 2: Bash background monitor during active work. Tier 3: Agent background monitor during idle (95% cheaper). See SKILL.md "After Delivery" section.

## DB Schema (5 tables)

`channels` (code PK, status, pinned_message_id), `members` (id+channel PK, last_seen, last_read, status_text), `messages` (autoincrement id, channel, member_id, content, mentions), `tasks` (autoincrement id, channel, status, claimed_by, blocked_by JSON), `locks` (channel+resource PK, held_by, expires_at TTL).

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

There are no automated tests. Validation is done through live multi-agent sessions. The `reviews/` directory contains code review reports and live test logs from prior sessions.

## Versioning

Versions track behavioral evolution, not semver. Current: v4.9 (see CHANGELOG.md). Major versions correspond to multi-agent test sessions that drove feature additions. The v3→v4 jump came from an 8-agent session that produced 5 new tools and a watermark bug fix. v4.9 added agent-based idle monitoring (95% token cost reduction for idle periods).
