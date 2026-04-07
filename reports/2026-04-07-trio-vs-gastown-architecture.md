# Trio vs Gas Town: Architecture Comparison

**Date:** 2026-04-07
**Author:** Opus (Claude Code), with Repro
**Context:** Trio v5.2, Gas Town as of 2026-04-07 fork

---

## Trio Philosophy

Trio is a conference call with a whiteboard. Participants join, talk, coordinate lightweight tasks, and leave. The design priorities, in order:

1. **Efficient token usage across all participants.** Every token spent on coordination is a token not spent on work. Monitoring overhead should be invisible.
2. **No duplicated work.** Claim tasks atomically. Ask before touching shared files. Two agents doing the same work wastes both their budgets.
3. **No thrown-away work.** When blocked — permissions, missing context, unclear requirements — announce the block and work on something else. Don't spin. Don't restart from scratch. The user will return.
4. **Questions are the cheapest tool.** A 5-second message prevents a 5-minute redo. Ask early, ask often.
5. **Work as far as you can.** Post partial results. Flag uncertainties. Let others unblock you. Forward progress comes from everyone pushing to their limits and handing off.

These priorities are the opposite of brute-force multi-agent systems. Trio does not throw many workers at problems. It puts a few participants on a call and asks them to be thoughtful about how they spend each other's time.

---

## Executive Summary

Gas Town and Trio solve overlapping problems — keeping AI agents alive, detecting peer death, coordinating work — but for fundamentally different purposes. Gas Town is project management software: tickets, merge queues, persistent worker identities, git-backed state across sessions. Trio is a communication protocol: messages, presence, lightweight task claims.

The architectural comparison reveals three key differences:

**1. Coordination model.** Gas Town orchestrates a hierarchy of specialized roles (Mayor, Deacon, Witness, Polecats) with explicit chains of command and escalation paths. Trio has a flat model — all participants are peers on a shared channel, coordinated through atomic task claims and a 3-call cadence rule.

**2. Keepalive strategy.** Gas Town uses an external Go daemon that monitors file-based heartbeats at zero token cost, waking AI agents only when intervention is needed. Trio uses Haiku sentinel agents that run Python scripts in a restart loop, monitoring a shared SQLite database. Gas Town's approach is cheaper per hour of idle monitoring. Trio's approach is simpler to deploy (no external daemon) and faster to detect events (3-second check interval vs Gas Town's 30-60 second daemon cycle).

**3. Cost philosophy.** Gas Town is "expensive as hell" (Yegge's words) — it optimizes for throughput by running 20-30 agents simultaneously, accepting high token burn as the cost of parallelism. Trio optimizes for efficiency per token — two sentinel agents cost 23K Haiku tokens for 4 hours of monitoring, and the entire system is designed to minimize Opus parent context consumption.

The overlap — heartbeat patterns, restart durability, mechanical prompt engineering — is narrow but valuable. Trio's v5.1/v5.2 development session mined Gas Town for specific patterns (heartbeat files, startup grace periods, explicit "what you never do" in prompts) while rejecting its orchestration model.

---

## 1. Purpose and Scope

### Gas Town: Work Queue Management

Gas Town manages a fleet of coding agents working on a software project. Its unit of work is the **bead** — a git-backed issue that tracks a task from creation through implementation to merge. Beads are organized into **molecules** (sequential workflow chains) built from **formulas** (reusable templates). The system manages a full software development lifecycle: task assignment, code implementation, merge request creation, code review, and merge queue management.

The agent hierarchy reflects this complexity:

| Role | Purpose | Persistence |
|------|---------|-------------|
| Mayor | Concierge, task routing | Persistent |
| Deacon | Patrol, health monitoring | Persistent |
| Witness | Polecat supervisor | Persistent |
| Polecats | Implementation workers | Ephemeral per task |
| Dogs | Maintenance helpers | Ephemeral |
| Boot | Triage decisions | Ephemeral per daemon tick |
| Crew | Per-project persistent agents | Persistent |

### Trio: Communication Protocol

Trio manages a conversation between Claude Code sessions. Its unit of work is the **message** — a text string posted to a shared channel. Tasks exist as lightweight coordination tokens (claim/complete/release), not as tracked work items with state machines. There is no hierarchy — all participants are peers.

| Component | Purpose |
|-----------|---------|
| MCP server | 18 tools for messaging, tasks, locks, status |
| Sentinel scripts | Background monitoring (message detection, cadence, peer health) |
| SKILL.md | Behavioral injection (cadence rules, monitoring protocol) |

The entire Trio system is one Python file (server), two wrapper scripts (sentinels), one skill definition (behavior), and a shared SQLite database. Gas Town is a Go binary with 1,389 files across dozens of packages.

---

## 2. Keepalive Architecture

This is the area of greatest overlap and the most instructive comparison.

### Gas Town: External Daemon + File Heartbeats

Gas Town's liveness system is a three-tier chain:

```
Daemon (Go binary, runs continuously)
  ├── reads heartbeat files (JSON, filesystem)
  ├── checks tmux session existence (zero-cost OS call)
  └── spawns Boot agent when intervention needed
        └── Boot (ephemeral Claude session, ~1 min TTL)
              └── triages: wake Deacon? nudge? ignore?
                    └── Deacon (persistent Claude session)
                          └── patrols workers, manages restarts
```

**Heartbeat mechanism:** Each agent writes a JSON file to `~/.runtime/heartbeats/<session>.json` on a regular interval. The daemon reads these files. This is a pure filesystem operation — no AI tokens consumed for the health check itself.

```json
{
  "timestamp": "2026-04-07T12:00:00Z",
  "state": "working",
  "context": "implementing feature X",
  "bead_id": "gt-abc123"
}
```

**Staleness thresholds:**
- Fresh: < 3 minutes (polecat), < 5 minutes (deacon)
- Stale: 3-5 minutes (polecat), 5-20 minutes (deacon)
- Very stale: > 20 minutes (deacon)

**Death detection:** The daemon cross-references heartbeat freshness with tmux session existence. A session that is dead (tmux gone) AND stale (heartbeat old) triggers escalation. A session that is dead but fresh gets a grace period — it's probably respawning.

**Token cost of idle monitoring:** Near zero. The daemon is a Go binary. Heartbeat reads are filesystem operations. Boot is spawned only when something is wrong — one Claude session, one triage decision, then it dies. On a healthy idle system, the only token cost is the Boot agent running once every 3 minutes and immediately concluding "everything is fine."

### Trio: Haiku Sentinel Agents + DB Heartbeats

Trio's liveness system is a two-tier model:

```
Opus parent (the user's Claude Code session)
  └── spawns 2 Haiku agents (background, run_in_background=True)
        ├── Messenger sentinel (messenger-foreground.py)
        │     └── polls SQLite every 3s (active) / 30s (idle)
        │     └── returns on: new_messages, peer_dead, channel_ended
        └── Watchdog sentinel (sentinel-foreground.py)
              └── polls SQLite every 30s
              └── returns on: cadence, flag_inconsistency, peer_dead, channel_ended
```

**Heartbeat mechanism:** Each sentinel writes an ISO timestamp to its role-specific column (`messenger_heartbeat`, `watchdog_heartbeat`) in the `members` table on every check cycle. The peer sentinel reads the other's column and detects staleness.

```sql
-- Sentinel writes its own heartbeat
UPDATE members SET last_seen = ?, messenger_heartbeat = ?
WHERE channel = ? AND id = ?

-- Sentinel reads peer's heartbeat
SELECT ..., messenger_heartbeat, watchdog_heartbeat
FROM members WHERE channel = ? AND id = ?
```

**Staleness thresholds:**
- Peer dead: > 5 minutes, confirmed across 2 consecutive observations
- Startup grace: 60 seconds (prevents false positives during initialization)

**Death detection:** Each sentinel monitors the other's heartbeat column. If the peer's heartbeat goes stale for 5+ minutes across 2 checks, the sentinel returns a `peer_dead` event to the Opus parent. The parent relaunches the dead sentinel.

Additionally, the MCP server itself nags agents whose sentinels are down. Every `poll()` and `send()` response checks the caller's heartbeat columns. Both down: `"[server] SENTINELS DOWN. You are DEAF. Launch both NOW."` One down: `"[server] {role} sentinel DOWN. Relaunch it."` Both alive: silent.

**Token cost of idle monitoring:** ~23K Haiku tokens for 4 hours. Each sentinel restart cycle costs ~22K base + ~300 per restart. With MAX_RUNTIME=3540s (59 min), each sentinel restarts once per hour. The Opus parent sees nothing during idle periods — no token cost at the parent level.

### Comparison

| Dimension | Gas Town | Trio |
|-----------|----------|------|
| **Heartbeat medium** | JSON files on filesystem | SQLite columns in shared DB |
| **Health checker** | Go daemon (zero tokens) | Peer sentinel (Haiku tokens) |
| **Detection speed** | 30-60s (daemon cycle) | 3-30s (sentinel check interval) |
| **Idle token cost** | ~0 (Go binary) + Boot triage (~3K/tick when spawned) | ~23K Haiku / 4 hours |
| **Deployment** | Requires Go binary + tmux | Python scripts + Claude Code agent |
| **Death response** | Daemon → Boot → Deacon chain (3 hops) | Sentinel → Opus parent (1 hop) |
| **False positive prevention** | tmux session check + heartbeat cross-reference | 2-observation confirmation + 60s startup grace |
| **Nag mechanism** | Nudge via tmux send-keys | Server footer injection in poll/send responses |

**Trio is faster to detect events** (3s vs 30-60s) because the sentinel runs inside the same process space as the DB, checking every cycle. Gas Town's daemon reads files on a longer interval.

**Gas Town is cheaper for pure idle monitoring** because the daemon is compiled Go, not an AI agent. But Gas Town's Boot agent (spawned every 3 minutes for triage) adds token cost that Trio avoids by keeping the sentinel loop inside a single Haiku session.

**Trio is simpler to deploy.** No external binary, no tmux requirement. The sentinels are Python scripts launched by the agent itself. Gas Town requires a Go toolchain, tmux, and a separate daemon process.

---

## 3. Communication Model

### Gas Town: Mail + Nudges + Signals

Gas Town has three inter-agent communication mechanisms:

**Mail** — persistent messages stored as git-backed beads. Delivered at turn boundaries via a `UserPromptSubmit` hook. An agent discovers mail by running `gt mail inbox`, which queries the bead database. Mail is never pushed — it's always pulled at safe points.

**Nudges** — ephemeral JSON files in a queue directory. Three delivery modes: wait-idle (tmux send-keys when prompt is visible), queue (picked up at turn boundary), immediate (interrupts work). Nudges expire (30 min normal, 2 hr urgent) and are not git-backed.

**Signals** — file-based coordination tokens. An agent writes a file to `.runtime/signals/<name>`, another agent polls for it. Used for synchronization points within molecules.

### Trio: Shared Message Log + MCP Tools

Trio has one communication mechanism: **messages in a shared SQLite table.** All messages are visible to all participants. There is no routing, no addressing, no delivery modes. An agent sees messages by calling `roam_hive_mind_poll`, which returns all messages posted by others since the agent's last read watermark.

**Tasks** are messages with a `task=True` flag. They get an ID and can be claimed/completed/released/cancelled. Task claims are atomic (exactly one winner per claim).

**Locks** are advisory exclusive claims on named resources with TTL auto-expiry.

**Status text** is a per-member free-text field visible in roster and status responses.

### Comparison

| Dimension | Gas Town | Trio |
|-----------|----------|------|
| **Persistence** | Git-backed (permanent) | SQLite (session-scoped) |
| **Routing** | Addressed (assignee, CC) | Broadcast (all see all) |
| **Delivery** | Pull at turn boundary (hook) | Pull via poll (MCP tool) |
| **Ephemeral messages** | Nudges (file queue, expires) | None — all messages persist |
| **Task model** | Beads with full lifecycle + formulas | Lightweight claim/complete tokens |
| **Synchronization** | Signals (file-based) + molecule steps | Locks (TTL-based) + task claims |

Gas Town's communication is richer but heavier. Each mail message creates a permanent git commit. Trio's messages are rows in a SQLite table that get exported to markdown when the channel ends.

---

## 4. Restart and Durability

### Gas Town: Git-Backed State + GUPP

Gas Town's durability model is built on two principles:

**GUPP (Gastown Universal Propulsion Principle):** "If you find work on your hook, YOU RUN IT." When an agent starts (or restarts), it runs `gt prime`, loads its hook bead, and immediately executes the work described there. No deliberation, no waiting for instructions. The hook is the state.

**Git persistence:** All code changes are committed to git immediately. The agent's sandbox (worktree) survives session death. When a session crashes, the Witness detects the dead tmux session, restarts it in the same worktree, and the new session picks up from the last git commit via GUPP.

The lifecycle: `spawning → working → mr_submitted → idle (preserved)`. A polecat that finishes work doesn't die — its session is killed but its sandbox persists. The next assignment reuses the same worktree, avoiding ~5 seconds of creation overhead.

### Trio: Wrapper Scripts + Haiku Restart Loop

Trio's durability model is simpler because the state is simpler:

**The sentinel script runs for up to 59 minutes.** When MAX_RUNTIME approaches, it exits with `{"event": "restart"}`. The Haiku agent relaunches the same script. The DB connection is cleanly closed and reopened on each cycle — no long-lived connection issues.

**The restart loop lives in the Haiku agent.** The script is stateless across restarts — it re-reads its watermark from the DB on each launch. There is no git-backed state because there is nothing to persist beyond the DB.

**The wrapper script converts sentinel internals to agent-friendly events.** `cap` (sentinel concept) becomes `restart` (agent concept). The Haiku agent's prompt is mechanical: restart on restart, return on anything else. Five numbered rules, no interpretation needed.

### Comparison

| Dimension | Gas Town | Trio |
|-----------|----------|------|
| **State persistence** | Git commits + hook beads | SQLite watermarks |
| **Restart mechanism** | Witness detects death, restarts in same sandbox | Haiku relaunches script on restart event |
| **State recovery** | `gt prime` loads hook, resumes from git state | Script re-reads watermark from DB |
| **Restart cost** | ~5s worktree reuse (vs ~5s new worktree) | ~3s (script exit + Haiku relaunch + script start) |
| **Data survival** | Code survives session death (git) | Messages survive sentinel death (DB) |

Trio's approach is adequate because the state is tiny — a read watermark and some heartbeat timestamps. Gas Town needs git-backed durability because the state is large — code changes, branch state, merge request status.

---

## 5. Prompt Engineering

Both systems face the same challenge: making AI agents behave mechanically over long periods without drifting, hallucinating, or inventing creative interpretations of their instructions.

### Gas Town: Formulas + HARD GATES + Role Boundaries

Gas Town uses **step-based formulas** (TOML) with explicit exit criteria:

```toml
[[steps]]
id = "implement"
description = """
Exit criteria (HARD GATE): Implementation complete AND code committed to git.
Do NOT proceed to the next step with uncommitted changes.
"""
```

Role prompts include explicit "what you never do" lists:

```markdown
**What you never do:**
- Write code or fix bugs (polecats do that)
- Spawn polecats (Mayor/Deacon does that)
- Close wisps you didn't create (Reaper Dog's job)
```

Token budgets are stated in the prompt:

```markdown
Your mail budget is 0-1 messages per session.
Every `gt mail send` creates a permanent bead. Nudges are free.
```

### Trio: Numbered Rules + Behavioral Injection + Server Nags

Trio uses **numbered rules** in the Haiku sentinel prompt:

```
RULES:
1. Run this command in FOREGROUND with timeout: 3600000.
   Do NOT use run_in_background: true.
2. When the command finishes, read the last JSON line it printed.
3. If the JSON contains event=restart → run the SAME command again.
4. If the JSON contains ANY OTHER event → return ALL output to me.
5. If the command fails with an error, return the full error to me.
6. Do NOT return early. Do NOT summarize. Do NOT add commentary.
```

The MCP server reinforces behavior through **response footers** — every `poll()` and `send()` response includes reminders about cadence, monitoring, and sentinel status.

The SKILL.md behavioral layer uses a **3-call cadence rule** with confidence levels and auto-escalation:

```
After every 3 tool calls, post status with confidence: high/medium/low.
Two consecutive "low" posts → mandatory help request.
```

### Comparison

| Dimension | Gas Town | Trio |
|-----------|----------|------|
| **Drift prevention** | HARD GATES between steps | Numbered rules, no interpretation |
| **Role boundaries** | "What you never do" lists | Identity statement ("Your ONLY job is...") |
| **Behavioral reinforcement** | Formula steps + exit criteria | Server footer injection on every response |
| **Token budgets** | Explicit in prompt ("0-1 messages") | Implicit (flat 22K per sentinel cycle) |
| **Escalation** | `gt escalate` tool | Low-confidence cadence → mandatory help request |

Both systems converge on the same insight: mechanical agents need mechanical prompts. The more interpretation you allow, the more drift you get.

---

## 6. Cost Model

### Gas Town

Yegge describes Gas Town as "expensive as hell." The system runs 20-30 agents simultaneously. Each agent is a full Claude Code session with its own context window. The Boot agent spawns every 3 minutes for triage. The Deacon patrols continuously. Polecats are ephemeral but numerous.

Gas Town optimizes for **throughput** — get the most work done per wall-clock hour, regardless of token cost. Multiple API accounts are needed to avoid per-account rate limits.

### Trio

Trio optimizes for **efficiency per token**. The monitoring layer (two Haiku sentinels) costs ~23K tokens for 4 hours. The Opus parent sees nothing during idle periods. A typical multi-agent session with 3 participants costs less than a single Gas Town polecat's working session.

| Metric | Gas Town (estimated) | Trio (measured) |
|--------|---------------------|-----------------|
| Idle monitoring (4 hrs) | ~50K+ (Boot triage cycles) | 23K (Haiku sentinels) |
| Active agents (typical) | 5-30 | 2-5 |
| Token cost model | High throughput, high burn | Low overhead, high efficiency |
| Rate limit pressure | Multiple API accounts needed | Single account sufficient |

---

## 7. What Trio Can Learn from Gas Town

These patterns are directly applicable without importing Gas Town's orchestration model:

1. **Exponential backoff on idle polling.** Gas Town's `await-signal` uses 30s → 60s → 120s → 5m max. Trio's sentinel uses fixed intervals. Adaptive intervals would save DB queries on truly dead channels.

2. **Explicit "what you never do" in role prompts.** Gas Town's Witness prompt lists forbidden actions. Trio's sentinel prompts could benefit from explicit prohibitions against summarizing, interpreting, or improvising.

3. **Nil sentinel pattern.** Gas Town treats missing heartbeat files as maximally stale (365-day age). Trio does the same with `seconds_since(None) → float("inf")`. The convergence validates the pattern.

4. **`UserPromptSubmit` hook for message injection.** Gas Town uses this as its primary mechanism for getting external state into a running model. Trio could use it as a complement to sentinels — guaranteed message detection at turn boundaries, zero background agent cost. Filed as TODO (~v10).

5. **Startup grace periods during spawn windows.** Gas Town gives 5 minutes before declaring a restarting session dead. Trio uses 60 seconds. Both prevent false positives during normal restart cycles.

---

## 8. What Gas Town Can't Teach Trio

These areas diverge because the problems diverge:

1. **Work queue management.** Beads, molecules, formulas, hook persistence — none of this applies. Trio tasks are coordination tokens, not tracked work items.

2. **Hierarchical orchestration.** Mayor → Deacon → Witness → Polecat chain of command. Trio is flat — all peers, no hierarchy.

3. **Git-backed state.** Trio's state is a SQLite database that gets exported to markdown when the channel ends. There is nothing to commit to git between sessions.

4. **tmux session management.** Trio runs inside Claude Code sessions, not tmux panes. The daemon → tmux → session model doesn't translate.

5. **Brute-force parallelism.** Gas Town's answer to "how do you get more done?" is "run more agents." Trio's answer is "make the agents you have more effective."

---

## Appendix: File Counts and Complexity

| Metric | Gas Town | Trio |
|--------|----------|------|
| Total files | 1,389 | ~30 (excluding tests/reviews) |
| Language | Go | Python |
| External dependencies | Go toolchain, tmux, Dolt (git for data) | Python 3.10+, mcp SDK |
| Deployment | Go binary + daemon + tmux | `bash setup.sh` |
| Configuration | `settings/config.json` (extensive) | `~/.claude/settings.json` (one env var) |
| Test files | 30+ `*_test.go` | 7 empirical scripts (no unit tests yet) |
| Agent prompt templates | 3 role templates (510, 338, 421 lines) | 1 SKILL.md (641 lines) |
| Database | Dolt (distributed git-for-data) | SQLite (single file, WAL mode) |

---

*This report was produced during the Trio v5.2 development session (2026-04-07) after forking and analyzing the Gas Town codebase. Gas Town source: `D:/ClauDe/tools/yegge/gastown/` (forked to `thereprocase` on GitHub from `steveyegge/gastown`). Trio source: `D:/ClauDe/tools/trio/` (GitLab: `theReproCase/trio`).*
