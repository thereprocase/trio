# Per-Process Server Capability as a Simplification Lever

**Date:** 2026-04-18
**Context:** Retrospective continuing from session-token-simplification. The core insight: Claude Code spawns one `python nth_server.py` process per MCP stdio connection. That process is, by construction, owned by exactly one Claude Code session. Module globals in that process are per-session state for free. A lot of what the current skill does manually can be replaced by exploiting this.

## The lever

**Each MCP server process = exactly one session.** No shared state between sessions at the process level; all cross-session coordination happens through the shared SQLite DB. This means:

- Session identity is implicit from process identity.
- Per-session state (member_id, channel, watermark, active task) lives safely in module globals.
- Session lifetime = process lifetime. Process dies = session ends.
- Nothing the client passes on a tool call is required for the server to know "which session is this."

The current design treats the server as if it were a shared stateless service where every call must carry (channel, member_id, session_token) so the server can figure out "who's calling." That's redundant with what the transport already tells us.

## Tier 1 — wins that fall out automatically

These need no change to the MCP transport, no harness feature request, no long-blocking primitive. Just use the process-per-session isolation.

### 1. Server-secret `session_id` (already captured separately)

See `reports/2026-04-18-session-token-simplification.md`. Mint `session_id` on `nth_connect`, stash in module global, never return to client. Bearer-capability semantics evaporate.

### 2. Implicit `member_id` on mutating calls

After `nth_connect`, the process knows its own `member_id`. Every subsequent `nth_send` / `nth_poll` / `nth_ack` / `nth_retract` / `nth_claim` call could ignore the client-supplied `member_id` and use the process-stored one. Client-supplied `member_id` becomes advisory (helpful for debugging, ignored for auth).

### 3. Implicit `channel` for single-channel sessions

Most sessions join one channel. After `nth_connect(channel="foo")`, the process could infer `channel="foo"` on subsequent calls. Multi-channel sessions use a dict keyed by channel in module globals. Either way, `channel` becomes optional on most RPCs.

### 4. Drop manual heartbeat

FastMCP exposes connection-close events. Server handles shutdown hook: mark all sessions belonging to this process as ended, release their task claims, emit `peer_dead` to channel peers. The `last_seen` column is now just audit data, not the liveness signal.

### 5. Drop the task lease sweeper

`_sweep_stale_leases` exists because we can't detect session death. With process-close handling (#4), leases release automatically when the claiming process dies. The `lease_expires_at` column and sweeper logic both go away.

### 6. Drop `revoked_at` and session revocation machinery

Nothing to revoke — a session ends when its process ends. The `revoked_at` column and the filtering clauses that check it all go away.

### 7. Drop most of the sentinel's adaptive-mode logic

`nth_sentinel.py` infers active/idle/sleep from string-matching `status_text` for sleeping keywords. With a simple `nth_set_mode(mode)` RPC that writes the explicit mode directly, the keyword-detection layer deletes itself. `status_text` becomes display-only.

## Tier 2 — wins that depend on MCP tool-call duration

These work **if** Claude Code's MCP client allows tool calls to block for long periods (hours) without timing out the parent's inference step. The MCP protocol allows tool calls to take arbitrary time; whether Claude Code enforces a shorter ceiling in practice is the open question.

### 8. Replace the whole sentinel architecture with `nth_wait`

Instead of spawning Haiku sub-agents that run blocking scripts, expose a server tool:

```python
@mcp.tool(...)
def nth_wait(timeout_seconds: int = 3600) -> dict:
    """Block until a new message, task update, or channel event arrives.
    Returns the event. Updates heartbeat while blocked."""
    ...
```

Client calls `nth_wait()` when idle. Server blocks until something happens or timeout. Returns to the client with the event. No sub-agent. No Haiku cost. Zero inference tokens burned during the wait — the parent's tool call is suspended, not running.

**What this eliminates:**
- Both sentinel sub-agents (`messenger-foreground.py`, `sentinel-foreground.py`)
- `nth_sentinel.py` entirely
- `agents/trio-sentinel.md`
- The `trio-sentinel` subagent template fix from v6.2 (moot — no sub-agents)
- SKILL.md's whole "Background Monitoring" section (~80 lines)
- The restart-loop convention
- Peek polls (subsumed by `nth_wait`)
- Capability-scoping bugs (no sub-agent = no impersonation surface)

### 9. Drop cadence enforcement

The 3-call cadence rule exists because peers can't see what you're doing between tool calls. With server-pushed events, peers know when you post, and the server can emit its own "session quiet for N minutes" event that fires `nth_wait` on peers. No client-side mechanical posting required.

### 10. Drop stay-connected discipline

"Do not disconnect when your work is done" is currently a behavioral rule in SKILL.md. With process-lifetime = session-lifetime, it's mechanical: the Claude Code session is alive iff the MCP server process is alive. The rule becomes "keep the Claude Code session open," which the user controls, not the skill.

## Tier 3 — cleanups that follow

### 11. SKILL.md collapses to ~80 lines

Rough shape:
- Frontmatter
- One-paragraph purpose
- Tools (one-line each): connect, send, poll, ack, retract, wait, end, claim/complete/cancel/release, set_status, set_mode, lock/unlock, history
- Post-connect: `nth_connect` then immediately `nth_wait` in a loop, processing each event as it returns
- Untrusted peer content
- Ask questions (behavioral, retained)
- No duplicated work (behavioral, retained)
- Task coordination (1 paragraph + table)
- Retracting
- Nav block to REFERENCE/DESIGN

### 12. PROTOCOLS.md mostly deleted

Sentinel event tables: gone. Cadence escalation: gone. Watermark recovery: simplified. What remains: task-lifecycle recipes, channel-recovery scenarios, retract policy. Probably merges back into REFERENCE.md.

### 13. DESIGN.md trimmed

Design philosophy retained. Rationale for cadence / sentinels retained as historical. Active guidance reduced — most of the "why" sections document decisions we're simplifying away.

## Tier 4 — still needed (not simplifiable by this lever)

- **Task coordination** (claim/complete/cancel/release) — shared-DB coordination primitive. Core function of nth.
- **Locks** — named-resource mutex with TTL. Core.
- **Status rendering** — user-facing affordance. Core.
- **Multi-session DB coordination** — the whole point. Core.
- **Untrusted peer content discipline** — safety rule, independent of mechanism.
- **Ask-questions / avoid-duplication / work-far-as-you-can** — behavioral philosophy, independent of mechanism.

## Final shape estimate

| Component | Current | Post-simplification | Delta |
|-----------|---------|---------------------|-------|
| `server/nth_server.py` | ~2300 lines | ~1200 lines | -48% (kill token validation, sweeper, auto-clear logic, adaptive inference) |
| `server/nth_sentinel.py` | ~400 lines | deleted | -100% |
| `server/messenger-foreground.py` + `sentinel-foreground.py` | ~130 lines | deleted | -100% |
| `agents/trio-sentinel.md` | 70 lines | deleted | -100% |
| `SKILL.md` | 170 lines | ~80 lines | -53% |
| `REFERENCE.md` | 200 lines | ~150 lines (merged w/ PROTOCOLS) | -25% |
| `PROTOCOLS.md` | 240 lines | merged or deleted | -100% |
| `DESIGN.md` | 95 lines | ~70 lines | -26% |

Rough total: ~3600 lines → ~1500 lines. 58% reduction across the whole skill + server codebase.

## What needs to be true for this to work

### Certainty check

- **FastMCP single-threaded stdio.** Module globals are safe with single-threaded event loop. Confirmed for stdio transport. For SSE, need per-connection state instead of module globals (FastAPI `request.state` or similar). Verify before SSE migration.
- **Connection close detection.** FastMCP needs to expose shutdown/disconnect hooks. If not, fall back to heartbeat-based liveness (what we have today) — degrades gracefully.
- **MCP tool call duration limits.** Tier 2 hinges on `nth_wait(timeout_seconds=3600)` being accepted by Claude Code's MCP client. Protocol allows it; Claude Code's enforcement is unclear. **Experiment needed.**

### If Tier 2 doesn't work

Tier 1 wins alone are worth doing. They:
- Eliminate the session-token bearer model (~30% server reduction)
- Move heartbeat to process-lifetime (kills sweeper, revocation machinery)
- Don't touch the sentinel architecture

Tier 2 is the bigger prize, but Tier 1 is independently worthwhile.

## Migration path (v7 proposal)

1. **Phase 1 (safe, no behavior change from user POV):**
   - Add `_live_sessions` module dict in nth_server.py.
   - `nth_connect` populates it, mints server-secret `session_id`.
   - `nth_send` / `nth_poll` / `nth_ack` / `nth_retract` / `nth_claim` look up `_live_sessions` on every call; use the server-known session if present, fall back to legacy path if not.
   - Client-supplied `session_token` becomes a no-op hint (ignored). Mark deprecated.
   - Drop `session_token` from connect response.
   - Add FastMCP shutdown hook that releases claims + marks sessions ended.
   - Test against current SKILL.md (which still passes tokens) — backward compatible.

2. **Phase 2 (SKILL.md update):**
   - Delete the Session token section from SKILL.md.
   - Delete `session_token=` from all example RPC calls in SKILL.md / REFERENCE.md / PROTOCOLS.md.
   - Deploy.

3. **Phase 3 (tier 2 experiment, gated on MCP duration test):**
   - Add `nth_wait(timeout_seconds=3600)` RPC. Verify Claude Code accepts long-blocking tool calls.
   - If yes: delete sentinel scripts, delete trio-sentinel agent, rewrite SKILL.md's monitoring section to a two-line `nth_wait` loop.
   - If no: stop at phase 2; file RFC upstream for long-blocking MCP tool support.

4. **Phase 4 (cleanup):**
   - Drop `revoked_at`, `role`, `lease_expires_at` columns (already unused).
   - Simplify `sessions` table to audit-only shape.

## Open questions

1. **What's Claude Code's actual MCP tool-call timeout?** Documented or empirical. If it's bounded (e.g., 30s or 60s), tier 2 needs a different mechanism (still-small nth_wait cycles chained by the server, with keepalive). If it's hours, tier 2 is free.
2. **FastMCP connection-close hook.** Is there one for stdio transport? If not, how does the MCP SDK surface EOF on the stdio pipe?
3. **Multi-channel sessions.** One process connecting to N channels is an edge case today. Does the dict-keyed-by-channel design feel right, or would a stack / single-current-channel model be cleaner in practice?

## References

- Session token simplification: `reports/2026-04-18-session-token-simplification.md`
- Sentinel simplification paths: `reports/2026-04-18-sentinel-simplification-paths.md`
- v6.2 council: `reviews/2026-04-17-v6.2-council-brainstorm.md`
- v6.2 Aragorn: `reviews/2026-04-17-v6.2-aragorn-security-review.md`
