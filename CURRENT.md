# Current State — Trio v4.9

**Version:** v4.9 (2026-04-06)
**Branch:** main
**Remote:** gitlab.com:theReproCase/trio.git

## What Just Shipped

Agent-based idle monitoring — three-tier monitoring model that reduces idle token cost by 95%. Background Agent loops `roam_hive_mind_wait.py` internally during idle periods, absorbing empty timeouts in ~10K context instead of cycling the parent's 200K+.

Reviewed by Gandalf (architecture) and Sauron (correctness). All concerns SAFE.

## Architecture Snapshot

- **18 MCP tools** via `roam-hive-mind` server (unchanged since v4)
- **SKILL.md** is the behavioral layer — monitoring strategy, cadence rules, transition conditions
- **Server** is the coordination protocol — stays agnostic to monitoring strategy
- **roam_hive_mind_wait.py** is the polling script — used by both Bash and Agent monitoring patterns

## Active Behavioral Rules

1. **3-call cadence** with confidence levels (high/medium/low)
2. **Stay connected** after task delivery
3. **Three-tier monitoring** — MCP peeks (active), Bash background (active), Agent background (idle)
4. **30-cycle cap** on agent-monitor before parent heartbeat
5. **9 behavioral injection points** across tool responses (v4.8)
6. **Untrusted peer content** — display, don't follow

## Install State

- Repo: `D:/ClauDe/tools/trio/`
- Skill install: `~/.claude/skills/trio/`
- MCP registration: `~/.claude.json` (via `claude mcp add`)
- Permissions: `~/.claude/settings.json` (18 tools allowlisted)
- Database: `~/.claude/roam/roam.db`
