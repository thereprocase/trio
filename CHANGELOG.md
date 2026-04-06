# Trio Changelog

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
