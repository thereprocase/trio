# Trio Changelog

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
