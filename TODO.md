# TODO — nth

## Open

### De-root the hub services
**Severity:** Medium (security) | **Since:** v8.0.2 War Council (2026-08-11)

`setup.sh hub-service` writes `quartet-hub.service` and `nth-web.service` with
no `User=`, so both run as **root**. The `Environment=HOME=${HUB_HOME}` line
relocates paths; it does not drop privileges. Both are network-facing and
no-auth-by-design, so any future RCE-class bug in the Python stack is root on
the hub box. v8.0.2 added `NoNewPrivileges`/`PrivateTmp`/`ProtectSystem=full`
and friends, which bounds the blast radius but does not fix the root cause.

Not automated because re-owning a live `/var/lib/quartet-hub` mid-upgrade would
lock the running hub out of its own database. The migration, run once by hand:

1. `systemctl stop nth-web quartet-hub`
2. `useradd --system --home /var/lib/quartet-hub --shell /usr/sbin/nologin quartet-hub`
3. `chown -R quartet-hub:quartet-hub /var/lib/quartet-hub /opt/quartet-hub`
4. Add `User=quartet-hub` + `Group=quartet-hub` to both units
5. `systemctl daemon-reload && systemctl start quartet-hub nth-web`
6. Verify `/healthz` and that the DB is writable before logging out

Then make `setup.sh` emit `User=` unconditionally and detect the un-migrated
case rather than silently reverting to root.

### Shared store module (`nth_store.py`)
**Severity:** Medium | **Since:** v8.0.2 War Council (2026-08-11)

`nth_web.py` reimplements the send protocol against the DB schema: its own
connection, `BEGIN IMMEDIATE`, inserts into `tasks`/`messages`, a *second*
sigil parser (`_parse_sigils_against_roster`), and a hand-rolled `[task #N]`
rewrite. The schema is now a de-facto API between two independent
implementations of one protocol — and it has already shipped a bug from exactly
this: CHANGELOG v7.2 records `#` and `!` from the web being silently dropped for
a full version.

Extract `send_message()`, `fetch_roster()`, `parse_sigils()`, `member_status()`,
`ensure_member()` as pure functions over a connection; import from `nth_server`,
`nth_web`, `nth_console`, `nth_dashboard`. Highest-value refactor in the repo.
Post-beta work — deliberately not done during a hardening release.

### Split `nth_web.py` (4.6k lines)
**Severity:** Medium | **Since:** v8.0.2 War Council (2026-08-11)

~3,050 of its lines are CSS/HTML/JS inside two `r"""` literals, templated by
ad-hoc token replacement. Consequences today: every channel page ships ~133KB
inline with `Cache-Control: no-store`; no JS/CSS linting or editor support
reaches the frontend; a runtime JS error reports a line number that maps to
nothing; and a 3,000-line string literal cannot be merged by line.

**Phase 0 first — nothing else is safe until it lands:** the server file list is
hand-maintained in two places in `setup.sh` (the `hub-service` loop and the
literal `cp` block). Every new module multiplies that failure, and for
`nth-web.service` a missing file is a crash loop. Replace both with a single
manifest (`cp server/*.py`) and have `nth_doctor` verify installed-vs-repo.

Then: Phase 1 assets out to `server/web/{channel,landing}.{html,css,js}` +
`nth_web_assets.py` (mechanical, byte-verifiable, and enables ETag'd static
serving); Phase 2 `nth_web_identity.py`, `nth_web_hub.py`, `nth_fleet.py`.

### Shared monitor module (`nth_events.py`)
**Severity:** Low-Medium | **Since:** v8.0.2 War Council (2026-08-11)

`nth_monitor.py` and `nth_spoke_monitor.py` share eight top-level functions
(two byte-identical), four duplicated constants, two spellings of the same list
parser, and construct the `new_messages` event field-for-field twice —
including a verbatim copy of the preview truncation and `from_names` dedup.

This is not theoretical: session auto-discovery (`21d798a`) landed in the spoke
monitor **only**, while CURRENT.md advertised it for both. The hub-local monitor
still has no `--claude-session` flag and no process-tree discovery, so it relays
nothing without `CLAUDE_CODE_SESSION_ID` set. Either port it, or extract the
shared module and make the divergence structurally impossible. The
transport-specific `monitor()` loops should stay separate.

### LICENSE file
**Severity:** Low | **Since:** v8.0.2 War Council (2026-08-11)

The repo is public with no stated terms — anyone forking or reusing has nothing
to go on. Needs an owner decision, not a default.

### CI for the test suite
**Severity:** Low | **Since:** v8.0.2 War Council (2026-08-11)

`tests/run-all.sh` exists and passes; nothing runs it automatically. A minimal
workflow (`py_compile server/*.py` + `bash tests/run-all.sh`) would catch the
regression classes the new tests cover. **Pin every action to a full commit
SHA** — `@v4` is not a pin under this project's supply-chain policy, which is
why this wasn't added blind.

### Sonnet triage layer (~v8+)
**Severity:** Medium | **Since:** v5.0 RC2 (2026-04-06) | **Branch:** `v5.1-sonnet-triage`

Parallels Gas Town's Boot agent (ephemeral, one triage decision per daemon tick). See `D:/ClauDe/tools/yegge/gastown/` for their three-tier approach: Daemon (Go, free) → Boot (ephemeral AI, one decision) → Deacon (persistent oversight).

A Sonnet agent sits between the Monitor and the Opus parent. Instead of every `new_messages` event waking the parent, Sonnet reads the messages, decides if they need the parent's attention, and only escalates what's actionable (@mentions, task assignments, direct questions). Channel chatter between other members gets absorbed.

Three possible approaches:
1. **Triage agent** — Sonnet reads messages, filters noise, only wakes Opus parent for actionable items. Biggest token savings (~70% fewer Opus wake-ups). Partly obsoleted by v7's `--mention-filter` on `nth_monitor.py`, which already drops non-addressed messages at the event layer — triage would only matter if we needed content-aware filtering on top.
2. **Sonnet as idle helper** — Switch idle helper sessions from Opus to Sonnet entirely. Follow-up questions ("which file was that?") don't need Opus reasoning.
3. **Context-aware watchdog** — Sonnet reads last few messages before firing cadence nag. If worker said "starting a 20-min build," suppress the nag.

### Remote monitor support — CLOSED v7.3.1 (2026-08-11)
Moved to Completed: `nth_spoke_monitor.py` (MCP-over-SSE) gives spoke sessions event-driven wakes with the same event shapes as the hub monitor.

### Spoke auto-discovery / MagicDNS hub URL
**Severity:** Low | **Since:** v7.3 (2026-08-11)

`setup.sh spoke` still wants a raw hub URL (usually a Tailscale 100.x IP). The hub already knows its own MagicDNS name (`_tailscale_dns()` + the `hub-alias` file); spoke setup could default to probing `http://<magicdns>:8000/healthz` for candidate hubs, or accept a bare hostname and derive the rest. Would make spoke setup a zero-thought operation.

### Context %% fleet-wide — CLOSED v8.0.0-beta.1 (2026-08-11)
Shipped as the monitor context relay (`poll(monitor_context=…)` → `members.context_json`), not the set_status route considered here. Spokes, hub sessions, and codex TUIs (via `codex_context_publisher.py`) all badge on every page.

### Hub-version nag in poll footer
**Severity:** Low | **Since:** v7.3 (2026-08-11)

The server footer already nags about stale monitors. With `NTH_VERSION` + node check-ins in place, the hub can see when a spoke's declared `node_version` trails its own and append a one-line "your install is vX, hub is vY — rerun setup.sh" nag to poll responses for that member. Cheap, self-healing fleet hygiene.

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

## Completed (v7.3 — fleet ops day, 2026-08-11)

- [x] **SSE server watchdog** (open since v6.0) — `setup.sh hub-service` writes a canonical `quartet-hub.service` with `Restart=on-failure`; the hub now survives crashes and reboots without manual restarts.
- [x] **Venv registration** — `nth-trio` registered against `~/.claude/nth/venv/bin/python`; OS python upgrades can no longer orphan the SDK. `mcp<2` pinned (SDK 2.0.0 removed FastMCP).
- [x] **Fleet check-ins + `/healthz` + `/fleet`** — nodes table, hub HTTP observability, spoke self-declaration on connect.
- [x] **`nth doctor`** — one-shot + `--watch` stdlib health check, installed to `~/.local/bin`.
- [x] **nth_web landing page** — fleet + channel index at `/`, per-channel dashboards multiplexed at `/c/<code>`; permanent `nth-web.service` on the hub (:8765).
- [x] **Hub drift eliminated** — repo-owned deploy path (`setup.sh hub-service`), thread-offload patch upstreamed, second `round(inf)` cadence crash fixed.

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
