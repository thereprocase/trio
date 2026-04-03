# Trio Changelog

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
