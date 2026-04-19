# TODO — nth

## Open

### Sonnet triage layer (~v8+)
**Severity:** Medium | **Since:** v5.0 RC2 (2026-04-06) | **Branch:** `v5.1-sonnet-triage`

Parallels Gas Town's Boot agent (ephemeral, one triage decision per daemon tick). See `D:/ClauDe/tools/yegge/gastown/` for their three-tier approach: Daemon (Go, free) → Boot (ephemeral AI, one decision) → Deacon (persistent oversight).

A Sonnet agent sits between the Monitor and the Opus parent. Instead of every `new_messages` event waking the parent, Sonnet reads the messages, decides if they need the parent's attention, and only escalates what's actionable (@mentions, task assignments, direct questions). Channel chatter between other members gets absorbed.

Three possible approaches:
1. **Triage agent** — Sonnet reads messages, filters noise, only wakes Opus parent for actionable items. Biggest token savings (~70% fewer Opus wake-ups). Partly obsoleted by v7's `--mention-filter` on `nth_monitor.py`, which already drops non-addressed messages at the event layer — triage would only matter if we needed content-aware filtering on top.
2. **Sonnet as idle helper** — Switch idle helper sessions from Opus to Sonnet entirely. Follow-up questions ("which file was that?") don't need Opus reasoning.
3. **Context-aware watchdog** — Sonnet reads last few messages before firing cadence nag. If worker said "starting a 20-min build," suppress the nag.

### Remote monitor support
**Severity:** Medium | **Since:** v6.0 (2026-04-09, then deferred through v7)

Make the event monitor work over MCP tools so it can run on remote `/quartet` spoke sessions. Currently hub-only because `nth_monitor.py` reads the local SQLite DB directly. Spoke sessions fall back to inline MCP peeks between work steps. A server-pushed event stream over SSE would restore parity.

### SSE server watchdog
**Severity:** Medium | **Since:** v6.0 (2026-04-09)

Auto-restart `quartet_server.py` if it crashes. Currently manual restart. Could be a systemd unit, a wrapper script with restart loop, or a process supervisor.

### UserPromptSubmit hook as monitor complement (~v10)
**Severity:** Low / Idea | **Since:** v5.1 (2026-04-07)

Inspired by Gas Town's `UserPromptSubmit` hook pattern (Yegge). A Claude Code hook on `UserPromptSubmit` could check the trio DB for unread messages at every turn boundary. If messages are pending, the hook returns `{"decision": "block", "reason": "You have N unread trio messages..."}` and Claude Code re-injects that as system context. Zero background processes needed for turn-boundary detection.

**NOT a replacement for the Monitor.** The hook only fires between turns — if Opus is mid-tool-call for 30 seconds, messages queue. The Monitor detects messages within 0.5s during active work. The hook would be a **complement**: Monitor for fast sub-turn detection, hook for guaranteed turn-boundary detection. Belt and suspenders.

**Why low priority:** v7's Monitor architecture already works reliably. The hook adds a second detection layer but doesn't solve a problem we currently have. Worth revisiting if we see turn-boundary staleness in practice.

**Reference:** Steve Yegge's Gas Town (`github.com/steveyegge/gastown`).

### Cadence rule enforcement gap
**Severity:** Medium | **Since:** v4.5 live test (2026-04-03)

The Monitor's cadence event is the mechanical backstop for model-level attention drift during absorptive tasks — it fires when a task-holder goes quiet past the threshold. The backstop is not the cure; cadence drift is a model behavior, and the fix there is better in-context discipline (announcing intent before extended reasoning, posting interim progress). See `reviews/v45-live-test/TODO-cadence-escape.md`.

### Conversation export quality
**Severity:** Low | **Since:** v4

The markdown export (`nth_end`) is functional but minimal. Task state changes aren't timestamped in the export. Lock acquisitions/releases aren't included.

### Model-tag on member rows
**Severity:** Low | **Since:** v7 dashboard (2026-04-19)

The dashboard's `Model` column is a placeholder dash — trio doesn't know which Claude model each member is running as. Agents could self-report on `trio_connect` via a new `model` field. Makes the dashboard meaningfully tier-aware (opus/sonnet/haiku), which matters when deciding who to expect fast vs slow responses from.

## Completed (v7 — Monitor architecture, 2026-04-19)

- [x] **Per-process server / sentinel simplification** — superseded by the Monitor-based single-process design. Each session launches one persistent `nth_monitor.py` via Claude Code's `Monitor` tool; no subagents, no Haiku, no two-sentinel peer-heartbeat dance.
- [x] **Delete deprecated sentinel files** — `nth_sentinel.py`, `nth_wait.py`, `messenger-foreground.py`, `sentinel-foreground.py`, `agents/trio-sentinel.md`.
- [x] **Tune polling** — 3s→0.5s active, 30s→3s idle, `PRAGMA synchronous=NORMAL` under WAL, heartbeat writes batched every 10s (30× margin over the 300s nag threshold).
- [x] **Enrich `new_messages` event** — carries `has_mentions` / `from_names` / `preview` so agents can skip the round-trip on cross-talk.
- [x] **`--mention-filter`** on `nth_monitor.py` — suppress wake-ups for messages targeted at other members.
- [x] **Cadence gated on held claimed task** — workers standing by for dispatch no longer get the "still idle" ping.
- [x] **Server noise reduction** — `_sentinel_nag` rewritten for Monitor context, footer only on poll responses, short-form task claim/complete in the channel.
- [x] **Long-duration sentinel soak test** — moot under v7; the monitor is a single persistent process, no restart loop to soak.
- [x] **Haiku sentinel reliability on idle channels** — moot under v7; no Haiku.
- [x] **Operator tooling** — `nth_console.py` (stdlib DB tailer, defaults to full-history dump so terminal scrollback is the chat history UI) + `nth_dashboard.py` (Rich per-agent engagement dashboard for 3-8 agent rooms).

## Completed (v5.0 RC2 — War Council)

- [x] Shared SLEEPING_KEYWORDS constant (roam_constants.py)
- [x] idx_messages_channel_member index for sentinel queries
- [x] War Council: 3 criticals fixed, SKILL.md contradictions resolved
- [x] Dual-sentinel pattern (message + watchdog) — superseded by v7 Monitor
- [x] Watchdog emergency protocol (relaunch BOTH on fire) — superseded by v7 Monitor
- [x] Internal looping (cap/error handled in-agent, never surface to parent) — superseded by v7 Monitor
- [x] Explicit FOREGROUND instruction in agent prompts (Haiku backgrounding fix) — moot
- [x] "Relaunch FIRST, process SECOND" rule — moot

## Completed (v5.0 RC1)

- [x] Unified sentinel (merges wait + watchdog) — superseded by v7 Monitor
- [x] `status_changed_at` column for transition tracking
- [x] `send()` auto-clears sleeping keywords
- [x] Flag inconsistency detection (2-observation threshold) — moot under single-process Monitor
- [x] Sleep confirmation (60s verified silence) — moot under single-process Monitor
- [x] SKILL.md simplified (~120 lines removed)
- [x] setup.sh deploys sentinel → v7 deploys `nth_monitor.py`

## Completed (v4.9)

- [x] Agent-based idle monitoring (95% token reduction) — superseded by v7 Monitor
- [x] Fix misleading watermark comment in `roam_hive_mind_poll`
- [x] Gitignore `settings.local.json` and `.env` files
- [x] Add `CLAUDE.md` project guide
