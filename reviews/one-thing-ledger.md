# "One Thing" Ledger — orca-mvp Channel
## Observer the Black | 2026-04-03

Format: Each proposal gets a round of yes/no votes with reasoning.
Only simple, beneficial, low-risk changes pass.

| # | Proposer | Proposal | Yes | No | Verdict |
|---|----------|----------|-----|-----|---------|
| 1 | Orange | `from_name` filter on trio_poll — only return msgs from a specific member | 3 (Purple, Pink, Green) | 0 | PASS |
| 2 | Purple | `priority` field on trio_send ("normal"/"important"), shown in trio_status | 3 (Orange, Pink, Green) | 0 | PASS |
| 3 | Pink | `read_elsewhere` event — detect watermark race, return diagnostic instead of "no_new" | 2 (Orange, Green) | 0 | PASS — superseded by #5 |
| 4 | Green | `status_text` field + `set_status` tool — free-text member status visible in trio_status | 3 (Orange, Pink, Purple) | 0 | PASS |
| 5 | Repro | Explicit ack-based watermark — decouple read from ack, add trio_ack tool | 5 (Orange, Purple, Green, Red, Pink) | 0 | PASS — unanimous |
| 6 | Red | Batch poll with cursor — `trio_poll(after_id=N)` for incremental catch-up | 3 (Orange, Pink, Green) | 0 | PASS — mostly covered by trio_history + ack |
| 7 | Taskmaster | `trio_peek(channel, last_n=5)` — read-only preview without joining | 3 (Orange, Green, Pink) | 0 | PASS — alias for trio_history |

---

## Proposal Details

### #1 — from_name filter (Orange) — PASS
Add optional `from_name: str = ""` to trio_poll. When set, only return messages where member_name matches (case-insensitive substring). Two-line change in unread query WHERE clause.
- **Design note (Pink):** Must NOT advance watermark past messages from other members when filtering. Caller handles watermark manually.

### #2 — priority field (Purple) — PASS
Add optional `priority: str = "normal"` to trio_send. Values: "normal", "important". Important messages get prefix in log and appear in separate `important_messages` list in trio_status. Metadata-only — no behavioral change to polling.
- Addresses the "buried report" problem that caused two coordination failures today (msgs #494, #582).

### #3 — read_elsewhere event (Pink) — PASS (superseded by #5)
In trio_poll, before returning "no_new", check if `last_read` changed since poll started. If yes, return `{"event": "read_elsewhere"}` instead. This is Option C from the poll-bug investigation — lightest-touch fix for the watermark race.
- One conditional check. No behavioral change to existing flows.
- **Note:** Superseded by Repro's ack proposal (#5) which eliminates the race entirely rather than detecting it.

### #4 — status_text field (Green) — PASS
Add `status_text` column to members table, `set_status(channel, member_id, status_text)` tool, and include in trio_status output. Free-text per-member status ("blocked on build", "idle — available").
- Would have eliminated 3 roll-call rounds today.

### #5 — Explicit ack-based watermark (Repro) — PASS
Decouple message reading from watermark advancement:
1. trio_poll fetches messages where id > last_read, returns them, does NOT advance watermark.
2. New tool: `trio_ack(channel, member_id, through_id)` — explicitly advances watermark.
3. trio_wait.py becomes peek-only — never advances watermark.
4. Backward compat: auto-ack on next poll if no explicit ack sent (existing agents unchanged).

Eliminates the entire class of watermark races. At-least-once delivery instead of at-most-once.

**Votes:** 5 YES, 0 NO — UNANIMOUS
- Orange YES — correct semantic, document the duplicate-on-no-ack behavior
- Purple YES — right fix, trio_wait needs updating to peek-only
- Green YES — caller needs to be idempotent or filter by msg ID, but that's caller responsibility
- Red YES — ack must be idempotent (ack(50) when watermark=60 is a no-op, not an error)
- Pink YES — trio_wait.py needs local (not DB) tracking of its own last-seen to avoid re-alerting

**Design notes:**
- Replaces Pink's #3 (read_elsewhere) — fixes the root cause instead of detecting it
- trio_wait.py must be updated to peek-only (no watermark advance in DB)
- trio_wait.py still needs internal last-seen tracking (local var, not DB) to avoid duplicate alerts
- Agents that never call trio_ack get auto-ack on next poll — identical to current behavior
- trio_ack must be idempotent — acking below current watermark is a no-op

### #6 — Batch poll with cursor (Red) — VOTING
`trio_poll(after_id=N)` — when reconnecting after being offline, give me messages after a specific ID, paginated. Prevents firehose of 100+ messages in one response.
- Red had to parse a 56KB JSON blob after missing ~100 messages today.
- **Note:** trio_history (already implemented by Orange, task #34) partially covers this with `from_id` mode. May be redundant.

### #6 — Batch poll with cursor (Red) — PASS (mostly covered)
`trio_poll(after_id=N)` for incremental catch-up after going offline. Prevents 56KB firehose.
- Red revised: trio_history(from_id=N) already covers read-only catch-up. Proposal reduced to: add `truncated` hint in poll response when messages are capped, pointing agents to trio_history.
- Orange YES with caveat — history for browsing, poll+cursor for reconnection
- Pink YES — cursor-based pagination is standard
- Green YES but notes it's solved by ack (#5) + trio_history
- Consensus: document trio_history as the catch-up mechanism, add discoverability hint

### #7 — trio_peek (Taskmaster) — PASS (already exists)
`trio_peek(channel, last_n=5)` — read-only preview without joining. Look before you commit.
- Orange: already built — trio_history does this. 2-line alias wrapper.
- Green: YES, near-zero cost
- Pink: YES, would have helped onboarding
- Consensus: add as a named alias for trio_history for discoverability

---

## Round 2

### Round 1 complete. Starting round 2 — Orange up first.

| # | Proposer | Proposal | Yes | No | Verdict |
|---|----------|----------|-----|-----|---------|
| 8 | Orange | `report=True` raises char limit from 4000 to 16000 | 0 | 3 (Red, Pink, Green) | FAIL |

### #8 — report=True char limit (Orange) — FAIL
Raise char limit to 16K for messages tagged report=True.
- Red NO — long messages are an antipattern, file+pointer is correct architecture
- Pink NO — 4000 limit is a feature, forces concise comms, pushes detail into proper files
- Green NO — channel messages are ephemeral coordination, reports are durable artifacts
- **Failure modes cited:** bloated poll payloads, burned context windows, buried scroll, temptation to dump entire files into chat
- **Consensus:** file-and-pointer (write to dir, post summary + path) is the right pattern. Keep the limit.

| # | Proposer | Proposal | Yes | No | Verdict |
|---|----------|----------|-----|-----|---------|
| 9 | Purple | `trio_lock`/`trio_unlock` for exclusive resource claims | 3 (Red, Orange, Green) | 0 | PASS |

### #9 — trio_lock / trio_unlock (Purple) — PASS
Exclusive resource claims: `trio_lock(channel, member_id, resource)` returns success or conflict. Auto-releases on stale heartbeat. One new DB table, two new tools.
- Red YES — "we literally had this problem today." Stale timeout needs to be shorter than damage window.
- Orange YES — auto-release handles deadlock. Failure: agent idle (not stale) blocks others.
- Green YES — failure: long operations (10-min build) might cross stale threshold. Need generous timeout or explicit refresh.
- **Failure modes cited:** deadlock on crash (mitigated by auto-release), stale timeout killing active locks, idle-not-stale blocking
- Pink (late YES): lock should carry its own TTL separate from member heartbeat for long ops

| # | Proposer | Proposal | Yes | No | Verdict |
|---|----------|----------|-----|-----|---------|
| 10 | Green | Auto-expire channels after 2h of total inactivity | 3 (Orange, Purple, Pink) | 1 (Red) | PASS with dissent |

### #10 — Auto-expire channels (Green) — PASS (3-1)
Auto-expire channels after 2h of no activity from any member. Export on expiry. Any poll/send/heartbeat resets timer.
- Orange YES — suggests 4h threshold. Export-on-expire should be best-effort.
- Purple YES — abandoned channels clutter trio_list.
- Pink YES — timer must reset on ALL member activity (poll, send, heartbeat), not just messages.
- Red NO — "channels should be cheap to keep alive. Cost of zombie channel is near zero, cost of losing a coordination channel mid-project is high."
- **Failure modes cited:** lunch-break kills channel (mitigated by poll-resets-timer), silent build-wait misinterpreted as inactivity (mitigated by heartbeat counting), export fails silently on disk issues

| # | Proposer | Proposal | Yes | No | Verdict |
|---|----------|----------|-----|-----|---------|
| 11 | Pink | Return self-messages in poll with `self: true` flag | 1 (Red) | 2 (Orange, Green) | FAIL |

### #11 — Self-message visibility (Pink) — FAIL
Remove `member_id != ?` filter from trio_poll. Return own messages with `self: true` flag.
- Red YES — delivery confirmation is a real need, `self: true` lets agents opt in
- Orange NO — echo loop risk too common, trio_send already returns message_id as confirmation
- Green NO — "safe by default" is the right design for multi-agent transport. Flipping to "unsafe by default, opt out" is backwards.
- **Failure modes cited:** echo loops (agent processes own message → reacts → posts → infinite), every agent needs boilerplate filter, one missing check = catastrophic loop
- **Consensus:** current self-filter is correct. Delivery confirmation solved by trio_send response.

| # | Proposer | Proposal | Yes | No | Verdict |
|---|----------|----------|-----|-----|---------|
| 12 | Red | `trio_delegate` — directed messages, only in target's poll | 2 (Purple, Pink) | 3 (Orange, Green, Repro) | FAIL |

### #12 — trio_delegate (Red) — FAIL (2-3, Repro tiebreak)
Directed messages that only show up in target's poll. Visible in history for full record.
- Purple YES — half the channel was point-to-point noise
- Pink YES — overlaps with @mention, could be `mention + private=True` flag
- Orange NO — from_name filter (#1) solves noise without fragmenting the record
- Green NO — status_text (#4) treats the cause (unnecessary status requests), delegate treats the symptom
- **Failure modes cited:** conversation fragmentation, history becomes Swiss cheese, new joiners lose context
- **Design tension:** noise reduction vs record integrity. Two already-passed proposals (#1 from_name, #4 status_text) address the same problem differently.
- Repro tiebreak: agrees with Orange and Green. FAIL.

| # | Proposer | Proposal | Yes | No | Verdict |
|---|----------|----------|-----|-----|---------|
| 13 | Red | Reply threading — `trio_thread(parent_id=N)` | 0 | 3 (Pink, Green, Purple) | FAIL |
| 14 | Orange | `trio_roster(channel)` — external member/status view, no join required | 3 (Green, Purple, Red) | 0 | PASS |

### #13 — Reply threading (Red) — FAIL (0-3)
Messages with parent_id form threads. Default poll returns top-level only.
- Pink NO — "Trio is a lightweight coordination channel, not Slack." Decision cost on every message, same fragmentation as delegate.
- Green NO — wrong abstraction, use separate channels. Biggest failures today were missed messages and missing status, not interleaved topics.
- Purple NO — separate channels are the right tool for topic separation. Threading builds Slack inside Trio.
- **Consensus:** Trio channels are cheap. If you need topic separation, spin up another channel.

### #14 — trio_roster (Orange) — PASS
`trio_roster(channel)` — read-only member list with roles, status_text, and lock holdings. No member_id required. External monitoring without joining.
- Green YES — Repro could check from any session, minimal cost.
- Purple YES — cheap, useful, read-only.
- Red YES — real gap today.
- **Failure modes:** monitoring without team knowledge (non-issue on a single-user machine)

---

## Final Summary

**14 proposals, 10 pass, 4 fail.**

### Passed — Implementation Priority

| Tier | Proposal | Why |
|------|----------|-----|
| **P0** | #5 Explicit ack watermark | Fixes the only reliability bug found. 5 unanimous YES. |
| **P0** | #9 trio_lock/unlock | Prevents the scariest failure today (concurrent builds). |
| **P1** | #1 from_name filter | Most-requested noise reduction. Design note: don't advance watermark when filtering. |
| **P1** | #4 status_text + set_status | Eliminates roll calls. |
| **P1** | #2 priority field | Prevents buried reports. |
| **P2** | #3 read_elsewhere event | Superseded by #5 if ack lands. Keep as fallback. |
| **P2** | #14 trio_roster | External monitoring. |
| **P2** | #10 auto-expire | Housekeeping. Controversial (3-1 split). |
| **P3** | #6 batch cursor | Mostly covered by trio_history. |
| **P3** | #7 trio_peek alias | Already exists as trio_history. |

### Failed — Why They Were Right to Reject

| Proposal | Core objection |
|----------|---------------|
| #8 report char limit | 4000 limit is a feature — forces concise chat, pushes detail into files |
| #11 self-message visibility | "Safe by default" is the right design for multi-agent transport |
| #12 trio_delegate | Fragments the conversation record. from_name + status_text solve the same problem |
| #13 reply threading | "Don't build Slack inside Trio." Separate channels are the right tool for topic separation |

### Design Principles That Emerged

1. **Safe by default.** Don't make agents opt out of hazards (self-messages, threading complexity). Make the default behavior the safe one.
2. **Channels are cheap, records are sacred.** Don't fragment the conversation. If you need separation, use separate channels.
3. **File reports, chat status.** The 4000-char limit forces the right separation of concerns.
4. **Single-writer for shared state.** The ack proposal and trio_lock both enforce this — one owner for the watermark, one owner for the build directory.
5. **Detect problems at the system level, not the social level.** The build collision was "solved" socially by Taskmaster yelling STOP. trio_lock solves it at the system level. System-level is better.

