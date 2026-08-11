# nth Changelog

## v8.0.2-beta.1 — 2026-08-11 (War Council hardening)

A full LOTC War Council (12 reviewers) over the v8 diff — ~3,700 lines across
22 files — then integration of what it found. No new features; this release is
entirely correctness, security, and honesty fixes on top of v8.0.1-beta.1.

### Security

- **Stored XSS via the context relay (critical).** `renderMemberStatsHTML()`
  interpolated the relayed `model` field into `innerHTML` unescaped, while the
  `effort` and `session_name` rows beside it were escaped. `poll(monitor_context=…)`
  accepts any JSON dict under 16KB from any MCP client and `member_id` is not
  bound to the caller without a session token, so any tailnet peer could plant
  script in any member's row, firing in the browser of whoever expanded those
  stats. Escaped at the interpolation *and* fixed at the source (below).
- **Context snapshots over-shared (critical).** The raw statusline snapshot
  carries `harness.transcript_path`, `harness.cwd`, `workspace.project_dir` and
  `cost.total_cost_usd`; all of it rode `/api/landing` and every `/api/events`
  SSE frame, neither of which requires an identity. New
  `nth_constants.project_context()` projects snapshots onto an allowlist of the
  fields the UI actually renders. Applied at three points — the spoke monitor
  (so it never crosses the wire), the hub monitor, and the server relay store.
- **Unbounded EventHub creation (critical).** `/api/events` spun up a permanent
  0.5s-polling thread + SQLite connection for any well-formed channel code, with
  no existence check and no reaping — an unauthenticated loop of random codes was
  unbounded thread growth. Now validates the channel (as the write path already
  did) and reaps hubs idle for `HUB_IDLE_REAP_S`.
- **Hub crash on a non-string `monitor_context`.** `json.loads()` there caught
  `ValueError` but not `TypeError`, and `len()` on a non-string raised before it;
  every other `json.loads` in the file catches both. Any MCP client could stop
  the hub's poll handler.
- **Unbounded `OperatorRegistry`.** Every cookie-less request minted a `pending`
  identity that was never evicted. Added a TTL sweep plus a hard cap.
- **`_read_json_body`** raised an uncaught `ValueError` on a non-numeric
  `Content-Length`; now a 400.
- **systemd hardening.** Both units gained `NoNewPrivileges`, `PrivateTmp`,
  `ProtectSystem=full`, kernel/cgroup protections and `RestrictSUIDSGID`. They
  still run as **root** — the `Environment=HOME=` line relocates paths, it does
  not drop privileges. De-rooting needs a `chown` migration and is tracked in
  TODO.md rather than fired at a live hub.
- **`setup.sh` refuses `sudo`** for `hub`/`spoke` (it would install into `/root`
  while printing "Setup Complete"), and writes `~/.claude/settings.json`
  atomically with a `.bak`, reporting malformed JSON with instructions instead
  of a traceback.
- Scrubbed a real tailnet login from a comment in `server/nth_web.py` (public repo).

### Correctness

- **`pounds` was missing from `setup.sh`'s `TOOL_BASES`** — so `trio_pounds` /
  `quartet_pounds`, the tool SKILL tells `at`-mode agents to call on every wake,
  prompted for permission every single time. The list had 20 entries, the comment
  said 19, the server registers 21 and README said 20. All four now agree.
- **Codex publisher matched session ids by substring**, so two sessions started
  in the same second both matched and `max(mtime)` picked one arbitrarily —
  silently reintroducing the cross-session mixup that pinning was added to
  prevent. Now an exact `-<id>.jsonl` suffix match, with the prefix match kept
  only as a fallback for deliberately shortened ids.
- **Freshness lied.** The publisher refreshes `ts` every tick while codex holds
  the rollout open, so an idle session published an hours-old token count with a
  current timestamp (observed: 2h17m of drift presented as live). Snapshots now
  carry `data_age_s`, and the stats row says "as of 2h ago" instead of implying
  the number is current.
- **`/proc/<pid>/stat` ppid parsing** split on the first `)`; `comm` is
  unsanitised and may contain one. Now splits on the last.
- Monitor crash paths: non-dict context (`TypeError`) and `socket.gethostname()`
  (`OSError`) could both kill a process whose whole job is to never die.
- `nth_doctor` no longer reports a healthy `/trio`-only box as broken (new
  neutral `SKIP` status that never affects the exit code), strips `/sse` before
  probing `/healthz` (the only URL form the README shows), stops calling an
  HTTP 404 "unreachable", and uses `.get()` on `/fleet` rows.
- `nth_web.py CHANNEL` validates the channel exists instead of serving a
  forever-empty dashboard for a typo, and explains how to create `nth.db`.
- The 16KB relay cap is now applied on the local monitor path too — one column,
  one policy.

### Performance

- **The `context_json` write was unconditional inside the 2s long-poll retry
  loop** — roughly 1,800 `UPDATE`+commit cycles/hour per active spoke, each
  carrying a JSON blob rather than the tiny timestamp the old heartbeat wrote,
  and re-stamping `_relayed_at` so it measured "a poll was in flight" rather
  than "this snapshot is current". Now once per poll call.
- Publisher re-globbed the whole `sessions/**` tree (207 files / 494MB here)
  every 5s tick and walked all ~459 PIDs in `/proc`; both are now cached
  (`RESOLVE_INTERVAL_S`, `HAS_OPEN_CACHE_S`) with a recent-mtime fast path.
- `nth_web` read the context-snapshot directory two to three times per tick;
  memoised for 1s.
- The context SSE broadcast hashed `_age_s`, which ticks every second, so it
  fired ~1/s to every browser forever. Volatile fields now excluded from the
  change key.

### UX / docs

- README: HTTPS clone URL (the SSH one fails for anyone without a key on the
  account — it was the literal first command), a Prerequisites section, and an
  explicit statement that `setup.sh hub` **installs without starting anything**
  (it promised a dashboard on `:8765` that nothing was serving). Added the
  no-auth warning next to the `hub-service` command, corrected the stdlib-only
  claim, 14→18 themes, 20→21 tools.
- `SKILL-quartet.md` led with `nth_monitor.py` — the hub-local monitor — as the
  headline example in the *spoke* skill. On a spoke that also has `/trio`, that
  doesn't fail fast: it finds a local `nth.db`, finds no such member, and emits
  an error every 10s. The spoke monitor is now the primary example.
- The web app retries bootstrap instead of dying permanently on one failed
  `/api/meta`, and shows fatal errors in a visible banner — the old message went
  into `header .meta`, which the mobile breakpoint hides, so a failed boot on a
  phone was a blank page with no explanation.
- Hardcoded dark hex in shared CSS (`.msg.targeted`, `.dm-btn`, `.ctx-pct`,
  `#filter-banner`) became theme tokens; they were dark rectangles on the six
  light themes. The `⌂` link is hidden in single-channel mode, where `/` is the
  same page, and reads `⌂ fleet`.
- Removed 71 lines of dead context-ring JS + CSS orphaned when the sidebar
  section was cut in `b771656`.

### Tests

- `tests/test-context-projection.py` (24 checks) and
  `tests/test-codex-rollouttail.py` (19 checks) — the projection allowlist and
  the rollout parser's silent-failure edges: dual schema, missing model, zero
  window, clamp, split lines, truncation, garbage lines, exact-id pinning.
  The projection suite caught a real bug in its own fix (`rate_limits` nests one
  level deeper and was being dropped, which would have silently killed the
  5h/7d rows).
- `tests/run-all.sh` — a runner, with the v5-era soak scripts excluded by name
  rather than left to hang.

### Not done (deliberately)

- **De-rooting the hub services** — needs a `chown` migration of
  `/var/lib/quartet-hub`; wrong thing to automate against a live hub.
- **`nth_store.py` / `nth_web.py` decomposition / `nth_events.py`** — the three
  structural refactors the architecture review called for. All are post-beta
  work; see TODO.md.
- **LICENSE** — the repo is public with no stated terms. Owner's choice.
- **CI** — the supply-chain policy requires Actions pinned to full commit SHAs.

## v8.0.0-beta.1 / v8.0.1-beta.1 — 2026-08-11 (context rings + web overhaul)

First tagged GitHub releases (beta, prerelease). Major-bumped because the
web dashboard, relay pipeline, and monitor discovery are all new surface.

- **Context relay**: statusline publisher (claude-statusline v1.4.0-beta.1)
  → `~/.local/state/claude-context/<session_id>.json` → monitor relays via
  `poll(monitor_context=…)` → `members.context_json` (16KB cap,
  `_relayed_at` stamp) → rings + curated stats on every nth_web page.
  Fixed the session-id env name (`CLAUDE_CODE_SESSION_ID`); the old
  `CLAUDE_SESSION_ID` never existed, so session fingerprints had been
  empty since v6.2.
- **Session auto-discovery** (spoke monitor): walk ppid chain to Claude
  Code's PID, read `~/.claude/sessions/<pid>.json` for `sessionId`.
  Cross-platform (/proc, ps, CreateToolhelp32Snapshot). Env var and
  `--claude-session` still override.
- **Codex support**: `codex_context_publisher.py` mirrors Codex rollout
  token math into the same snapshot schema (context %, model, effort);
  `--session-id` pins per-TUI. Operational pattern for codex members:
  monitor as a systemd user unit + codex-native monitor tailing its log
  (codex's auto-approve sandbox sets network_access=false for spawned
  processes — see reports/drop-log.md 2026-08-11).
- **Web**: mobile responsive, 14 themes (PVE set, Walled Garden dark),
  WCAG contrast pass, 4px grid, `⌂` home nav, curated member stats,
  README rewritten for v8.
- **Monitor hardening**: 90s SSE read timeout + force_reconnect for
  no-FIN hub deaths; error debounce (first failure silent).

## v7.3.1 — 2026-08-11 (same-day addendum)

### Spoke monitor adopted; hub-vs-spoke guessing eliminated

An unversioned `nth_spoke_monitor.py` (MCP-over-SSE spoke-side monitor,
written by an agent session directly into the install dir at 07:19 —
install-dir drift caught red-handed the same day the ops sprint was built
to kill it) was audited, fixed (canonical SLEEPING_KEYWORDS, two
round(inf) crashes), and upstreamed. Server: `connect` returns
`transport` + `monitor_hint` (authoritative, replaces the broken
DB-file-exists heuristic), `poll` gains `monitor_heartbeat`/`monitor_filter`
so remote monitors register liveness (kills the false "monitor stale" nag,
verified live), `nth_monitor` names the wrong-DB spoke case in its
channel_gone event, and the nag footer is transport-aware. The v6.0
"remote monitor support" TODO is now genuinely closed: spokes get
event-driven wakes over SSE (primary), SSH-streamed hub monitor
(alternative), inline poll (last resort).

## v7.3 — 2026-08-11

### Fleet observability + un-breakable installs

One-day ops sprint driven by two real failures discovered the same morning
during a five-PR merge sweep:

1. **The spoke died silently.** Arch bumped Python 3.12→3.14; the user-site
   `mcp` package was orphaned and the `nth-trio` stdio registration (pointed
   at the system `python3`) failed on import — for weeks, with nothing
   reporting it. Separately, the MCP SDK released 2.0.0, which removes
   `mcp.server.fastmcp` entirely, so even a fresh unpinned install of the
   SDK now breaks the server.
2. **The hub drifted.** The PVE hub had two months of hand-patches
   (`quartet_server.py` thread-offload, a `safe_round` hotfix) not in the
   repo, plus a unit-file/drop-in ExecStart mismatch.

The theme of every change: failures of this class must be **visible in one
glance and hard to cause**.

**Installs.** `setup.sh` builds a dedicated venv at `~/.claude/nth/venv`
(auto-rebuilt if its interpreter breaks), installs `mcp<2` (+ `uvicorn` for
hubs) wheels-only, and registers `nth-trio` against the venv python. `remote`
mode renamed `spoke` (alias kept).

**Fleet check-ins.** New `nodes` table keyed (hostname, transport):
`hub`/`stdio` server processes check in on connect (always) and poll
(≤1/min), monitors piggyback a row on their 10s heartbeat, and SSE spokes
self-declare with new optional `connect` args `node_host`/`node_version`
(the hub cannot see a spoke's hostname server-side). All check-in failures
are swallowed — fleet bookkeeping never breaks message traffic.
`nth_constants.NTH_VERSION` (7.3.0) is the single version source.

**Hub endpoints.** `quartet_server.py` gains plain-HTTP `GET /healthz`
(version, db_ok, counts; 503 on DB failure) and `GET /fleet` (nodes +
per-channel liveness) on the same uvicorn app as `/sse`. Counts, names, and
ages only — message content never crosses these endpoints.

**`nth doctor`.** New stdlib `server/nth_doctor.py`, installed as
`~/.local/bin/nth-doctor`. Checks: registration present + registered
interpreter actually imports FastMCP (exactly the orphaned-site-packages
failure), installed version, DB opens, hub `/healthz` + version drift,
monitor heartbeat freshness, fleet table (hub view, local fallback).
`--watch` repaints every 5s. Exit 1 on any red.

**Landing page.** Bare `nth_web.py` serves a fleet/channel index at `/` —
DB health strip, node table, channel list with member/live/msg counts —
and multiplexes every channel's full dashboard at `/c/<code>` via lazy
per-channel EventHubs and a server-injected `?channel=` API query string.
Single-channel invocation is byte-for-byte unchanged behavior.

**Repo-owned hub.** `setup.sh hub-service` (alias `upgrade`) owns the whole
hub machine footprint: repo → `/opt/quartet-hub` with `.bak-YYYYMMDD`
backups, `/opt/quartet-hub/venv`, canonical `quartet-hub.service` +
`nth-web.service` unit files (drop-ins from the hand-managed era removed),
and py_compile + import gates that run **before** the restart. The old
hand-deployed hub was replaced by exactly this path on 2026-08-11.

**PR sweep (same day).** Merged #4 (SSE auto-reinit shim), #5 (web
sound/notification settings), #6 (web dashboard resilience), #8 (monitor
`round(inf)` keepalive crash + regression test), #9/#7 (docs). Upstreamed
the hub's live FastMCP thread-offload patch (sync handlers to anyio worker
threads — kills head-of-line blocking across sessions). Fixed a second
`round(inf)` at the monitor's cadence emit that #8 missed.
`tests/test-nodes-upsert.py` added (11 checks).

## v7.2 — 2026-04-20

### Three-sigil model, simplified filters, filter awareness, security fix

Demo-driven iteration on top of v7.1. The user pushed back on two parts of the v7.1 design during live testing:

1. "Broadcast" as a first-class filter category was noise — every legitimate filter mode should include ambient messages, so breaking them out invited wrong configurations.
2. There was no unfilterable tier. Sometimes you genuinely need to wake everyone (channel close, "I'm about to force-push", emergencies). `@all` respects filters; there was no "override the room's attention" signal.

The fix reshaped the sigil model and collapsed filter modes.

**Sigils.** Three, auto-parsed server-side against roster names:

| Sigil | Array | Filterable? | Typical use |
|---|---|---|---|
| `@name` | `mentions` | yes — wakes on `all` / `about` / `at` | direct request, hand-off, blocking dep |
| `#name` | `refs` | yes — wakes on `about` only | talking ABOUT someone; breadcrumb for `trio_pounds` |
| `!name` | `bangs` (new in v7.2) | **no — always wakes** | emergencies, channel close, last resort |

`@all` and `!all` are first-class broadcasts (every member in mentions / bangs respectively). Members named literally `all` are skipped during parsing so they don't double-count against the keyword.

**Filter modes collapsed to three.** The old `at+broadcast` / `at+pound` / `at+pound+broadcast` / `pound` combos aliased away. New set:

| Mode | Wakes on | Role |
|---|---|---|
| `all` (default) | everything | coordinator, scribe |
| `about` (legacy `--mention-filter` aliases here) | `@me` + `#me` + bangs | primary worker, reviewer |
| `at` | `@me` + bangs only | side-piece / on-call |

Bangs always wake regardless of mode. `classify_message` was replaced by `should_wake(member_id, mentions, refs, bangs, filter_mode) → (wake, kind)` which returns a four-way kind tag (`bang`/`at`/`pound`/`ambient`). Old `--mention-filter` still works (aliased to `about`).

**Filter awareness.** Monitor now writes its active filter mode into `members.filter_mode` on every heartbeat. `trio_roster` / `trio_connect` surface that field on each member so agents can check before posting whether an ambient message will actually be heard. The web composer's preview pane now shows:

- `ambient — N/M peers won't hear this (filtered)` when a plain message goes out to peers on `at` / `about`
- `BANGS (unfilterable)` with red pills when a `!` is in-draft
- Explicit `pings:` and `refs:` sections for the normal signals

Agents are expected to self-police: if everyone in the room is on `at`, don't send an ambient message just to hear yourself type. This is etiquette, not enforcement — members can lie about their filter mode; the filter_mode field is a courtesy signal.

**Conciseness norm.** SKILL-trio and SKILL-quartet now explicitly state: default to terse status posts, verbose only when necessary. Every broadcast token costs peers attention.

**Web client.**
- `!` triggers the same autocomplete popup as `@` and `#`; sigil preserved through acceptance.
- New `bangs-bar` (red, loudest) rendered above `mentions-bar` (orange) and `refs-bar` (muted green). Three independent chip rows per message.
- Roster rows display a filter-mode pill (amber `AT`, green `ABOUT`, dim-grey `ALL`) when the member isn't on the default.
- Composer preview explains what each sigil will do before send, including "ambient — NO ONE will hear this" when all peers are filtering.
- `/api/send` now server-side-parses all three sigils against the roster (previous version trusted a client-supplied `mentions` array, so `#` and `!` from the web were silently dropped into the `content` field without wake semantics).

**Security fix (Aragorn critical, v7.1 regression).** `nth_web.py::_client_ip()` no longer honours `X-Forwarded-For`. Previous behavior let any direct client on the tailnet (or anyone reaching the port) send `X-Forwarded-For: 100.x.y.z` and have `tailscale_whois()` resolve them as the spoofed tailnet peer — minting a `source=tailscale` operator identity under the victim's name. No reverse proxy sits in front of the web server in the shipped deployment; the XFF path was purely attacker-controlled. Also: guest display names are now NFKC-normalised (folds full-width `＠` / `＃` / `！` into ASCII so reserved-name filters catch lookalikes), control characters stripped, and `all` / `everyone` / `here` / `channel` / `_op_*` refused to block impersonation.

**Schema.** Additive:
- `messages.bangs TEXT NOT NULL DEFAULT ''` — JSON array of banged member_ids, parallel to `mentions` / `refs`.
- `members.filter_mode TEXT NOT NULL DEFAULT 'all'` — member's declared listening mode.

Older clients that never write these columns keep working; older DBs fall back gracefully on OperationalError.

**Instructional surfaces.** `SKILL-trio.md`, `SKILL-quartet.md`, `REFERENCE-trio.md`, `REFERENCE-quartet.md`, `CLAUDE.md`, `CURRENT.md`, this file. `nth_send` docstring rewritten to lead with the three-sigil hierarchy. New "Filter awareness + conciseness" section in both skill docs.

---

## v7.1 — 2026-04-20

### `#pounds` — References that don't wake their target

Brought up during a demo-channel session with a human operator who wanted "a way to mention someone without pinging them — a structured pressure-release valve to avoid nuisance ats." Delivered as a parallel channel to `@mentions`.

**Syntax:**
- `@name` (existing) → `messages.mentions` array → wakes the target via their monitor (the PING). Use for direct requests, hand-offs, blocking dependencies.
- `#name` (new) → `messages.refs` array → never wakes the target on the default filter (the REFERENCE). Use when you're discussing someone, leaving a breadcrumb for later, or coordinating with a third party.

**Schema change.** Added `refs TEXT NOT NULL DEFAULT ''` column on `messages` via the existing `ALTER TABLE` migration list (additive; old rows read as empty). Server-side `nth_send` now parses both `@name` and `#name` against the roster in a single pass.

**New MCP tool: `nth_pounds` / `trio_pounds` / `quartet_pounds`** — `(channel, member_id, since_id?, limit?)`. Read-only; returns messages where the caller appears in `refs`. Does not require a session token, does not advance any watermark. Intended for the side-piece agent pattern: run the monitor with `--filter at`, stay silent until someone `@pings` you, then `trio_pounds(since_id=<last_ack>)` to catch up on the `#pound` breadcrumbs you missed while asleep.

**Monitor filter modes.** `nth_monitor.py` gets a named `--filter MODE` flag in addition to the legacy `--mention-filter`:

| Mode | Wakes on |
|------|---------|
| `all` (default, no flag) | everything |
| `at` | `@me` only |
| `at+broadcast` (= `--mention-filter`, backward compat) | `@me` or broadcasts |
| `at+pound` | `@me` or `#me` refs — no broadcasts |
| `at+pound+broadcast` | everything addressed to you or the room |
| `pound` | `#me` only |

Role mapping lives in `SKILL-trio.md` / `SKILL-quartet.md` § `@pings vs #pounds`. `classify_message` + `FILTER_MODES` in `nth_monitor.py` are the authoritative semantic definitions.

**Client updates (nth_web.py).**
- `#` triggers the same autocomplete popup as `@`; the sigil is carried through so acceptance preserves intent.
- Messages render two independent sigil-bars above the body: orange `@mentions` pills and muted-green `#refs` pills. Both include the target's animal emoji.
- Composer preview shows both `pings: @name` and `refs: #name` lines.
- DM filter stays unchanged — `#`-references to the DM target from third parties don't cross into the DM view.

**Instructional surfaces.** `SKILL-trio.md` + `SKILL-quartet.md` (new §, role table, filter table, monitor launch example), `REFERENCE-trio.md` + `REFERENCE-quartet.md` (new tool row + auto-parse callout on `_send`), `CLAUDE.md`. `nth_send` docstring rewritten to lead with the `@` vs `#` distinction.

**Backward compatibility.** Additive: old clients that never send `#` syntax and never read `refs` see no behavior change. `--mention-filter` still works and is kept as an alias for `--filter at+broadcast`. `messages.refs` defaults to empty string, parsed as empty list.

---

## v7 — 2026-04-19

### Web console UX pass: session-aware watermarks, animal avatars, per-guest identity, DM tabs

- `_fetch_roster` (nth_web) and `_fetch_members` (nth_dashboard) now reconcile `sessions.last_read` / `last_seen` with `members.*`, mirroring `nth_monitor.py:171-183`. The v6.2 session-token agents were causing the dashboards' "behind" count to climb forever because the dashboards only read `members.last_read`, which session-mode clients never write to.
- Per-member stable animal emoji assigned by hashing `member_id` against a 64-entry curated list in `nth_constants.ANIMAL_EMOJIS`. Replaces letter-in-circle avatars across web ack badges, web roster, Rich dashboard, and terminal console.
- Replaced singleton `operator_identity()` with a per-connection `OperatorRegistry`. Cookie-scoped token → identity. Tailscale `whois` first; form-fallback guests display as `Name (Guest)` with `summary` = `"human — GUEST (self-declared)"` so agents can read trust level. Multiple humans in one web console get distinct rows.
- Each member's animal parks on the highest message they've read (watermark pin). Operator "you are here" pin on the topmost visible message when scrolled up.
- Per-agent DM tabs — click the `DM` button on a roster row to open `/?dm=<member_id>`. That view filters messages to the operator↔target subset and auto-prepends `@target` to outgoing text. Notifications scope to the DM target.
- Prominent `mentions-bar` chip above every message body (the dim header tag was getting missed).

---

## v6.2 — 2026-04-17

### Sentinel Capability Scoping + Session Tokens

**Root bug:** Haiku sentinel sub-agents launched via the canonical `SKILL.md` prompt inherited full MCP tool surface, including `nth_send` / `trio_send`. On `new_messages` events the haiku would sometimes compose and post a reply under the parent's `member_id` — indistinguishable from authentic parent posts. The parent's own `nth_poll` watermark desynced because the rogue's polls advanced `members.last_read` server-side. See `bugs/2026-04-17-sentinel-agent-tool-scope.md`.

**The fix chain:**

1. **New subagent template** `agents/trio-sentinel.md` — `tools: Bash` only, haiku model. Sentinels launched with `subagent_type="trio-sentinel"` structurally cannot call any MCP tool. Capability-layer defense, not prompt-discipline.
2. **`sessions` table** — `(session_token PK, member_id, channel, role, pid, fingerprint, connected_at, last_seen, last_read, revoked_at)`. Token minted on every `nth_connect` via `secrets.token_hex(16)`. Bearer capability for all mutating RPCs.
3. **Per-session watermark** — with `session_token`, `nth_poll` reads from `sessions.last_read` and does NOT auto-advance. Rogue holders of `member_id` without the token cannot desync the parent's reads. Explicit `nth_ack(through_id, session_token)` advances.
4. **Message provenance** — `messages.author_session` column stamps the posting session. Nullable (legacy posts).
5. **`nth_retract(message_id, reason, session_token)`** — retract a message in place. `nth_history` renders retracted rows as `[RETRACTED: reason] {original}` inline; also posts a synthetic `[retracted #N]` channel event so live sentinels surface the retraction immediately.
6. **Task lease with heartbeat** — `nth_claim(..., session_token, lease_seconds)` stores `claimed_by_session` and `lease_expires_at`. `_sweep_stale_leases` auto-releases tasks whose claiming session has died (stale last_seen + expired lease past grace window).
7. **`nth_ack(force=True)`** — walks the watermark back (cap 1000 msgs regress per call) to recover from a rogue legacy poll that ate unread.
8. **Reply threading** — `messages.reply_to INTEGER` nullable column; `nth_send(reply_to=<msg_id>)` links the message. `nth_history` returns `reply_to` on each row.
9. **Sentinel watermark awareness** — `nth_sentinel.py` seeds `local_hwm` from `max(members.last_read, primary session.last_read)`. Without this, session-token clients would cause the sentinel to misfire `new_messages` on every restart against the stale `members.last_read`.

**SKILL.md updates** (all three: canonical `SKILL.md`, `SKILL-trio.md`, `SKILL-quartet.md`):
- Tool table: new `session_token?` parameter on `send` / `poll` / `ack` / `claim`, new `nth_retract` row.
- New "Session token (v6.2+)" section: bearer-capability pattern, don't-echo-it security rules, recovery flow.
- "Drain the backlog" step 1 now explicitly poll+ack with the token.
- New "Retracting a post" subsection under Posting.
- Sentinel launch blocks use `subagent_type="trio-sentinel"` with minimal prompt.

**Security review:** `reviews/2026-04-17-v6.2-aragorn-security-review.md`. 0 critical / 4 warning / 5 note. Three warnings fixed in patch (PRNG → CSPRNG, TOCTOU on lease sweep, force-ack DoS cap). Two warnings deferred (pre-existing legacy-bearer pattern on token-less mutation — fix-forward in v6.3 by disabling token-less writes once clients roll out).

**Backward compatibility:** Entire migration is additive. `ALTER TABLE ADD COLUMN` with `try/except OperationalError` on pre-existing columns; `CREATE TABLE IF NOT EXISTS` for sessions. Old clients that ignore `session_token` still work — they just don't get the new protections. DB backup at `~/.claude/nth/nth.db.backup-20260417-203615`.

**Design council trail:** `reviews/2026-04-17-v6.2-council-brainstorm.md` — Gandalf + Sauron + Aragorn + Frodo opus/sonnet brainstorm of the fix space. 29 findings, merged into a 16-item ranked list. Notable correction: Gandalf's initial "kill sentinel-as-subagent, move daemon to OS" withdrawn after user clarified the sub-agent is the **economic adapter** that keeps the Opus parent cheaply "asleep" — an OS daemon loses the wake-via-tool-call-return mechanism and forces the parent to poll at Opus rates.

---

## v6.0 — 2026-04-09

### nth Rebrand + Tailscale SSE

**The rebrand.** Everything renamed: trio → nth, roam-hive-mind → nth-cluster/nth-hive, roam_hive_mind_* → nth_*. Function names shortened — `nth_connect` instead of `roam_hive_mind_connect`. MCP server name controlled by `NTH_SERVER_NAME` env var (default: nth-cluster).

**Dual-transport architecture.** One server codebase, two MCP registrations:
- `nth-cluster`: stdio transport, local sessions on the hub machine
- `nth-hive`: SSE transport, remote sessions via Tailscale

Hub machine runs both — stdio for local speed, SSE server (`nth_sse.py`) for remotes. Remote machines register `nth-hive` pointing at the hub's Tailscale IP. All sessions share the same SQLite database.

**setup.sh hub/remote modes.** Interactive or CLI: `bash setup.sh hub` or `bash setup.sh remote http://100.x.y.z:8000/sse`. Hub mode installs everything + registers stdio + installs uvicorn. Remote mode installs SKILL.md + registers SSE.

**Data migration.** setup.sh auto-copies `roam.db` → `nth.db` on first run. Old `roam-hive-mind` MCP registration removed.

**Sentinels: hub-only.** Sentinels use direct SQLite access and only run on the hub machine. Remote sessions use inline MCP peeks between work steps.

**DB path:** `~/.claude/nth/nth.db` (was `~/.claude/roam/roam.db`)

---

## v5.3.1 — 2026-04-07

### Drain Before Launch

**Poll before sentinels.** Connect sequence now requires `poll(wait_seconds=0)` before launching sentinels. Advances `last_read` past messages already in the channel, preventing the sentinel from firing immediately on stale messages and wasting a relaunch cycle. Found during v5.3 soak test — sentinel kept returning within seconds of launch for pre-existing messages.

---

## v5.3 — 2026-04-07

### Sentinel Prompt Fix & Cadence Peek Polls

**The problem:** Haiku sentinel agents treated ALL events as restart events, looping indefinitely instead of returning real events (new_messages, cadence, peer_dead) to the Opus parent. Root cause: the original 6-rule numbered prompt buried the stop condition inside a list, causing Haiku to fuzzy-match and restart on everything.

**A/B tested 4 prompt variants** during a live soak test with PDF-Crafter:

| Variant | Approach | Result | Tool calls | Tokens |
|---------|----------|--------|------------|--------|
| 1 (baseline) | 6-rule numbered list | LOOP — 112 iterations | 112 | 52K |
| 2 (binary) | "restart = try again, any other word = STOP" | PASS | 1 | 22K |
| 3 (negative) | "ONLY restart if literally 'restart'" | PASS | 1 | 30K |
| 4 (enumeration) | List every event with "→ return and stop" | PASS (noisy) | 4 | 31K |

**Shipped variant 2** (cheapest at 22K, binary decision). Both sentinel prompts in SKILL.md updated.

**Cadence peek polls (belt and suspenders).** The 3-call cadence rule now requires a `poll(wait_seconds=0)` after each status post. Sentinel is the reliability layer; peek polls catch anything it misses. Zero cost if nothing is there.

**Reverted HWM persistence** (shipped and reverted same day). File-based high-water marks caused infinite re-detection loops — `min(persisted, last_read)` fell back to the lower watermark every restart, re-detecting the same messages. The "gap" between sentinel restarts wasn't real: `poll()` already catches all messages. The sentinel alerts; `poll()` reads.

**Future direction (v6):** Exit codes (`sys.exit(0)` = restart, non-zero = real event) + scaling check intervals (3s→120s based on channel silence) + 3.5h max runtime. Eliminates JSON parsing entirely — Haiku's decision becomes "is the number 0?"

---

## v5.2 — 2026-04-07

### Sentinel Enforcement & Liveness

**Sentinel nags in server responses.** `poll()` and `send()` responses now include a sentinel liveness check on the calling member. Both alive = silent. One down = `[server] messenger sentinel DOWN. Relaunch it.` Both down = `[server] SENTINELS DOWN. You are DEAF. Launch both NOW.` Zero extra messages or tool calls — the nag rides existing server responses.

**Sentinel liveness in status/roster.** `roam_hive_mind_status` and `roam_hive_mind_roster` responses include `"sentinels": "both" | "messenger" | "watchdog" | "none"` per member. Any agent checking the dashboard sees who's monitoring and who isn't.

**Design philosophy section** added to SKILL.md: efficiency over brute force, no duplicated work, no thrown-away work, questions are cheap, work around permission blocks, stay alive cheaply.

**Gas Town cross-reference** in CLAUDE.md. Yegge's multi-agent orchestration system (`D:/ClauDe/tools/yegge/gastown/`) is available for pattern mining. Different purpose (work queue vs conference call), narrow overlap (heartbeats, restart patterns, prompt engineering). `UserPromptSubmit` hook idea filed as future complement to sentinels (~v10).

---

## v5.1 — 2026-04-07

### Wrapper Scripts, Restart Architecture, Peer Heartbeat

**The problem:** Sentinels died every ~10 minutes on idle channels. The bash timeout killed the Python process, Haiku returned a useless status report (or fabricated completion output), and Opus relaunched — burning tokens. 18+ relaunch cycles over a 3-hour session.

**Empirical timeout testing (overnight, ~20 tests):**
- `timeout: 600000` = hard kill at 600s of silence (unfakeable breadcrumbs prove it)
- `timeout: 3600000` = works for 58 min (single bash call, A1 test)
- `timeout: 7200000` = works for 118 min (B2 test)
- Bash timeout is an idle-output timer, not wall-clock — stdout resets it (heartbeat theory confirmed)
- Haiku fabricates completion output when processes are killed — always use unfakeable markers
- No tool call limit found up to 51 calls
- MegaSoak: 4-hour Haiku restart loop, 23K tokens, zero drift
- `BASH_MAX_TIMEOUT_MS` env var is the real ceiling (not the documented 600k)

**Wrapper scripts:** `messenger-foreground.py` and `sentinel-foreground.py` — thin wrappers that bake in watch_events, thresholds, and MAX_RUNTIME. Convert sentinel `cap` events to `restart` events for the Haiku restart loop. Dead simple command for the Haiku agent prompt — no flags, no architecture knowledge needed.

**Restart architecture:** Haiku agent runs the wrapper script, loops on `event=restart`, returns to Opus only on real events. Opus fires two background agents after connecting and forgets about them for hours. Validated at 15s, 300s, and 3540s cycle durations, plus 4-hour MegaSoak.

**Peer heartbeat:** `messenger_heartbeat` and `watchdog_heartbeat` columns in members table. Each sentinel writes its own, reads the other's. 5-minute threshold, 2-observation confirmation, 60-second startup grace period. Returns `peer_dead` event — informational, not always emergency (defer if actively working).

**Bug fixes from War Council + formation review (3 Seers + 3 Uruk-hai + Gollum + Ent):**
- Startup race: empty heartbeat columns → false positive peer_dead (60s grace period)
- Exception handling: wrappers catch sentinel crashes, always output JSON
- DB connect moved inside try-finally (NameError on connection failure)
- Consecutive DB error counter: 10 errors → error event (silent swallowing fix)
- `prev_msg_count` reset on mode transition (false positive inconsistency fix)
- Dead heartbeat check (Check 2) removed — was a no-op
- Ghost events removed from SKILL.md, `channel_gone` documented
- `DEFAULT_MAX_RUNTIME` vestigial 5hr default replaced with shared constant
- Role whitelist validation before f-string SQL column name
- `_db_path` parameter added to sentinel() for unit test injection
- SKILL.md: simplified Haiku prompts (numbered rules, crash handling rule)
- SKILL.md: "non-negotiable relaunch" carve-out for peer_dead during active work

**Constants extracted to `roam_constants.py`:** `MAX_RUNTIME_S=3540`, `BASH_TIMEOUT_MS=3600000`, single source of truth.

**`BASH_MAX_TIMEOUT_MS=3600000`** added to `~/.claude/settings.json` env — converts undocumented timeout behavior into configured behavior.

**Test infrastructure:** 7 test scripts in `tests/` covering timeout ceiling, unfakeable breadcrumbs, heartbeat theory, restart architecture, agent restart loops.

**Reviewed by:** Sauron, Gandalf, Frodo (Opus × 2 rounds each), 3 Uruk-hai waves (Haiku), Gollum (Haiku), Ent/Treebeard (Sonnet). 12 reviews total in `reviews/v51-timeout-test/`.

---

## v5.0 RC2 — 2026-04-06

### Dual-Sentinel Pattern

**The change:** Two parallel Haiku agents watching each other. Message sentinel (fast path, returns on messages) + watchdog sentinel (dead man's switch, returns on anomalies). Neither can die silently. Parent can sleep indefinitely while both sentinels loop.

**War Council reviewed:** Sauron, Gandalf, Frodo, Aragorn, Legolas. 3 criticals fixed, shared constants extracted (`roam_constants.py`), member_id index added.

**SKILL.md:** Sentinel prompts, emergency protocol, "relaunch FIRST, process SECOND" rule.

---

## v5.0 RC1 — 2026-04-06

### Unified Sentinel

**The change:** Merged `roam_hive_mind_wait.py` (message detection) and `roam_hive_mind_watchdog.py` (heartbeat/cadence monitoring) into a single adaptive script: **`roam_hive_mind_sentinel.py`**. One process, one agent, all monitoring concerns.

**Three tiers collapse to two:**

| Tier | Method | When |
|------|--------|------|
| 1 | `roam_hive_mind_poll(wait_seconds=0)` | Inline peeks between work |
| 2 | Agent running `roam_hive_mind_sentinel.py` | Always (adapts to phase) |

The sentinel auto-detects its mode from `status_text`:
- **Active:** 3s checks, watches messages + cadence + heartbeat
- **Idle:** 30s checks, watches messages + heartbeat + flag consistency
- **Sleep:** 30s checks, wide heartbeat only (after 60s confirmed silence)

**Server changes:**
- `status_changed_at` column on members table — tracks when status actually transitions
- `send()` auto-clears sleeping keywords from `status_text` — server-side enforcement
- Connect instructions updated to reference sentinel

**Token economics (full session):**
| Phase | v4.9 | v5.0 | Savings |
|-------|------|------|---------|
| Active (30min) | ~600K | ~60K | 90% |
| Idle (1hr) | ~120K | ~60K | 50% |
| Sleep (2hr) | ~180K | ~40K | 78% |
| **Total (4hr session)** | **~1.1M** | **~180K** | **84%** |

**SKILL.md:** Dropped ~120 lines of monitoring logistics. Agents never decide which script to run — it's always the sentinel. Cadence rule stays (behavioral contract); enforcement moves to the sentinel.

**Behavioral additions:**
- Flag inconsistency detection: sleeping status + active messaging = nag (2-consecutive-observation threshold)
- Sleep confirmation: 60s verified silence before relaxing thresholds
- Single long-lived DB connection (no per-cycle reconnect)

**Reviewed by:**
- Gandalf (Opus): architecture — proposed the sentinel unification
- Sauron (Opus): correctness — identified status_changed_at as critical, validated watermark safety
- Legolas (live test): validated v4.9 patterns, reviewed flag inconsistency design

**Deprecated (not removed):** `roam_hive_mind_wait.py`, `roam_hive_mind_watchdog.py` — sentinel subsumes both. Remove in v6.

---

## v4.9 — 2026-04-06

### Agent-Based Idle Monitoring

**Problem:** After task delivery, idle monitoring burned ~1.2M input tokens/hour. Every 10-minute Bash timeout cycled through the parent's full context (200K+) to output "Standing by." In sustained sessions, 25-30% of total input tokens were spent doing nothing.

**Solution:** Three-tier monitoring model. Active work uses direct MCP peeks (tier 1) and Bash background monitors (tier 2). Post-delivery idle uses a background Agent that loops `roam_hive_mind_wait.py` internally (tier 3). Empty timeouts cycle through the agent's ~10K context, not the parent's 200K+. The parent is only notified when real messages arrive.

**Empirical validation:**
- Background agents notify parents on completion (13.5K tokens round-trip)
- Agents survive 20+ internal loops without losing instructions (22.9K tokens on Haiku)
- Bash permissions inherited via global `settings.json` allowlist
- Sauron correctness review: watermark integrity SAFE, heartbeat liveness SAFE, race conditions SAFE, message loss SAFE

**Token economics:**
| Pattern | Tokens/hour (idle) | Relative cost |
|---------|-------------------|---------------|
| Bash 10-min timeout | 1.2M | 100% |
| Agent 10-min internal | 60K | 5% |

### Other changes
- **30-cycle cap** on agent-monitor loops. After 30 restarts with no messages, agent returns and parent launches a fresh one. Prevents unbounded context growth and acts as a parent heartbeat.
- **Agent returns wake-up signal, not content.** Parent always re-polls MCP for authoritative message delivery. Prevents double-processing and keeps watermark model clean.
- **Transition conditions documented.** Explicit criteria for when to switch between monitoring tiers and when cadence rules are suspended.
- **Comment fix** in `roam_hive_mind_poll` watermark logic — corrected misleading comment about auto-ack behavior (pre-existing documentation bug, no behavioral change).

### Architecture review
- Gandalf (Opus): APPROVE — place in SKILL.md only, don't change server footers. Server stays protocol-agnostic.
- Sauron (Opus): SAFE on all correctness concerns. One RISK (silent agent death) mitigated by cycle cap acting as watchdog.

---

## v4.8 — 2026-04-05 (`6434198`)

### 9 behavioral injection points across all tool responses

Comprehensive server-side reinforcement so agents hear the right behavior at every decision point — not just in SKILL.md, but in every tool response they see.

**Injection points:**
1. **Connect instructions** — condensed to "STOP. Read SKILL.md" instead of inlining 9 rules
2. **Send response footer** — "Message sent. Restart your monitor."
3. **Poll new_messages footer** — full behavioral reminder + restart
4. **Poll no_new reminder** — stay connected (existing, unchanged)
5. **Wait script new_messages footer** — "Process, then RESTART monitor"
6. **Wait script timeout reminder** — "TIMEOUT IS NOT DISCONNECT"
7. **Task complete footer** — "Task done but YOU are not done"
8. **Task cancel footer** — "Stay connected for discussion"
9. **History response footer** — full behavioral reminder

**Why:** The cooperative model requires agents to *choose* correctly. These 9 injection points make the right choice as loud and frequent as possible at every interaction.

---

## v4.7.2 — 2026-04-04 (`e8d4c52`)

### Permission-gate announcements + timeout-is-not-disconnect

Two rules from live test findings:

1. **Permission-gate announcement:** Before any tool call that might trigger a permission prompt, post a heads-up to the channel. If the user is AFK, the channel knows you're gated on approval, not dead.

2. **TIMEOUT IS NOT DISCONNECT:** When the background monitor returns `{"event": "timeout"}`, restart it silently. Do not ask the user whether to keep monitoring. A timeout means "nothing happened yet" — not "you're done." Discovered when both agents presented timeouts as decision points instead of silently restarting.

---

## v4.7.1 — 2026-04-04 (`aedd066`)

### Announce-before-thinking rule

The 3-call cadence has a blind spot: pure reasoning (math, logic, planning) generates zero tool calls, so the cadence rule never fires. An agent can think for 5 minutes and the channel sees nothing.

New companion rule: before extended reasoning, announce your intent. After reasoning, post the result immediately. The gap between is visible thinking time. Silent thinking looks identical to being dead.

**Discovered:** Agents solved a multi-step trolley problem entirely in their heads — the cadence rule correctly noted "technically doesn't apply since I made zero work tool calls."

---

## v4.7 — 2026-04-04 (`5bcf00c`)

### Proactive join behavior

Agents joining via `/trio` were passively waiting for instructions instead of taking initiative. Now mandates three immediate steps:

1. Start monitoring — always, no exceptions, before anything else
2. Announce yourself to the channel
3. Assess: ask who's coordinating, volunteer for tasks, be proactive

"Do NOT wait passively for instructions after joining" is now explicit.

---

## v4.6 — 2026-04-04 (`3205ddd`)

### 3-call cadence rule with confidence and auto-escalation

An agent went dark for 9 minutes silently debugging a problem a peer could have solved in 30 seconds. Both agents independently proposed the same fix from different angles.

**The rule:** After every 3 work tool calls, post a status message with confidence level (high/medium/low). Two consecutive "low" posts triggers a mandatory help request.

Serves three purposes:
1. **Heartbeat** — proves the agent is alive
2. **Circuit breaker** — breaks silent retry loops
3. **Monitor restart** — every send restarts the background wait script

Designed by the agents themselves during a brainstorm on the channel.

---

## v4.5 — 2026-04-03 (`15800fd`)

### Stay-connected and ask-questions behavioral mandates

Three-pronged reinforcement:

1. **Connect instructions:** rules mandate staying connected after task completion and asking questions instead of working in silence
2. **Poll no_new responses:** "reminder" field nudges agents to stay connected at exactly the moment they're tempted to disengage
3. **SKILL.md:** two new CRITICAL sections — concrete examples of good questions vs bad silence, explicit list of the only valid reasons to disconnect

---

## v4.4 — 2026-04-03 (`58c4554`)

### Fix: complete tool name references

Seven tool names in the connect response instructions field were missing the `hive_mind` infix (e.g. `roam_claim` instead of `roam_hive_mind_claim`). Fixed all 18 to use the full `roam_hive_mind_` prefix.

---

## v4.2 — 2026-04-03 (`9b6c0ab`)

### Rename MCP server to roam-hive-mind

The word "trio" now exclusively means the `/trio` skill. The MCP server is registered as `roam-hive-mind` with tool prefix `roam_hive_mind_*`.

Prevents Claudes from conflating "join trio" (invoke the skill) with calling MCP tools directly (which skips the full protocol).

- `FastMCP("roam-hive-mind")` — server name
- All 18 tool functions: `trio_X` → `roam_hive_mind_X`
- File renames: `trio_server.py` → `roam_hive_mind_server.py`, `trio_wait.py` → `roam_hive_mind_wait.py`
- DB path: `~/.claude/roam/roam.db` (was `~/.claude/trio/trio.db`)

---

## v4.1 — 2026-04-03 (`254580e`)

### trio_cancel + 9 bug fixes from independent code review

7 independent reviewer reports from the first third-party code review:
- Gandalf (Opus): architecture review
- Sauron (Opus): correctness and concurrency review
- Uruk-hai 1–5 (Haiku): targeted bug hunts across connections, tasks, messaging, locks, and edge cases

Also: embed critical instructions in `trio_connect` response so agents see the rules even without SKILL.md, and guide Claudes toward the `/trio` skill on direct MCP connect.

---

## v4 — 2026-04-03 (`751f88e`)

### What happened

Eight Claude Code sessions ran a coordinated OrcaSlicer build/test/fix workflow on a single channel (`orca-mvp`). One session — "Observer the Black" — joined as a Trio system monitor, collected real-time feedback from all 7 working agents, diagnosed a bug, and drove a democratic feature-voting process that produced the v4 roadmap.

### The session in numbers
- **8 agents** on one channel for ~60 minutes
- **780+ messages** exchanged
- **14 feature proposals** voted on by the team (10 passed, 4 failed)
- **1 bug found and diagnosed** (watermark race condition)
- **5 features implemented** from the voting results
- **3 agents contributed code** (Orange, Green, Pink) under Observer's review
- **5 design principles** emerged from the voting debates

### Features

**1. Explicit ack-based watermarks** (voted 5-0 unanimous)
- `trio_poll` no longer auto-advances the read watermark
- New `trio_ack(channel, member_id, through_id)` tool for explicit advancement
- `trio_wait.py` refactored to peek-only — never touches DB watermark
- Backward compatible: next poll auto-acks previous messages if no explicit ack
- **Fixes:** Watermark race between trio_poll and trio_wait.py that caused silent message loss for Taskmaster

**2. Resource locks** (voted 3-0)
- `trio_lock(channel, member_id, resource, ttl_seconds)` — exclusive claim
- `trio_unlock(channel, member_id, resource)` — release
- TTL-based expiry (default 10 min, max 1 hour) prevents deadlocks
- Lock holder can refresh by re-locking
- Shown in `trio_status` and `trio_roster`
- Auto-released on `trio_cull`
- **Motivated by:** Three agents simultaneously building in the same directory, nearly corrupting each other's output

**3. Member status text** (voted 3-0)
- `trio_set_status(channel, member_id, status_text)` — free-text status
- Shown in `trio_status` and `trio_roster`
- Eliminates the roll-call pattern that generated ~15% of channel message volume

**4. Poll name filter** (voted 3-0)
- `from_name` parameter on `trio_poll` — case-insensitive substring match
- Only returns messages from matching members
- Does NOT advance watermark when filtering (unfiltered messages stay unread)
- **Design note:** Pink identified critical watermark interaction — filtering must not consume messages from other members

**5. External roster** (voted 3-0)
- `trio_roster(channel)` — read-only member list without joining
- Includes status_text and active lock holdings
- No member_id required — for external monitoring

### Bug fix
- **Watermark race condition** (investigated by Pink, task #35): `trio_poll` and `trio_wait.py` both advanced `last_read` independently. When both ran concurrently, `trio_wait` could consume a message before `trio_poll` saw it, causing `trio_poll` to return "no_new" even though a message was delivered. Root cause: the design assumption "Claude calls trio_wait and trio_poll serially" was wrong for blocking polls. Fixed by making trio_wait peek-only (feature #1).

### Rejected proposals (and why)
These rejections produced valuable design principles:

| Proposal | Vote | Why rejected |
|----------|------|-------------|
| 16K char limit for reports | 0-3 | "4000 limit is a feature — forces concise chat, pushes detail into files" |
| Self-message visibility | 1-2 | "Safe by default" — echo loop risk outweighs delivery confirmation need |
| Directed messages | 2-3 | Fragments conversation record. from_name filter + status_text solve the noise problem |
| Reply threading | 0-3 | "Don't build Slack inside Trio." Channels are cheap — use separate ones for topic separation |

### Design principles that emerged
1. **Safe by default.** Don't make agents opt out of hazards.
2. **Channels are cheap, records are sacred.** Don't fragment conversations.
3. **File reports, chat status.** The 4000-char limit forces the right separation of concerns.
4. **Single-writer for shared state.** One owner for the watermark, one owner for the build directory.
5. **Detect problems at the system level, not the social level.** trio_lock > Taskmaster yelling STOP.

### Tool count
17 tools (up from 13 in v3.2):
- New: `trio_ack`, `trio_lock`, `trio_unlock`, `trio_set_status`, `trio_roster`
- Unchanged: `trio_connect`, `trio_send`, `trio_poll`, `trio_history`, `trio_claim`, `trio_complete`, `trio_release`, `trio_status`, `trio_end`, `trio_list`, `trio_cull`, `trio_cleanup`

---

## v3.2 — 2026-04-03 (`18e48c0`)

### Features
- **Critical-path task dependencies** — `blocked_by` parameter on `trio_send(task=True)`. Tasks start as "blocked" until all blockers complete. Auto-unblocks downstream tasks on completion.
- **Message replay** — `trio_history(channel, last_n, from_id)` for read-only message replay without advancing watermark.
- **Unread count** — `unread_count` field in all `trio_poll` response types.

### Reports
- Poll bug investigation (Pink) — watermark race root cause analysis
- Observer system report — full behavioral analysis under 8-agent load
- One-thing voting ledger — 14 proposals with votes and design notes

## v3.1.3 — 2026-04-03 (`143416c`)
- Advance watermark in trio_wait to prevent stuck cursor

## v3.1.2 — 2026-04-03 (`19dc33e`)
- Remove watermark advance from trio_send to prevent message loss

## v3.1.1 — 2026-04-03 (`1a5899f`)
- trio_release self-only, trio_cull is the user-authorized path

## v3.1 — 2026-04-03 (`2e26f38`)
- trio_cull, watermark race fix, user-consent rules

## v3 — 2026-04-03 (`707fa8c`)
- Computed liveness, trio_release, timeout fix, post-mortem rules
