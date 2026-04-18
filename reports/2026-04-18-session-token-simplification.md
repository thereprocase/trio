# Session Tokens Are Over-Engineered for the Actual Threat Model

**Date:** 2026-04-18
**Context:** Retrospective on v6.2. Observation: the `trio-sentinel` subagent template (tools: Bash only, no MCP) structurally fixed the live rogue-post incident. The session-token bearer-capability system we shipped alongside it was belt-and-suspenders, and some of its machinery could be simplified in a future pass.

## The observation

The live bug: haiku sentinel sub-agent inherits full MCP tool surface, sees `nth_send` and a channel/member_id in its prompt, occasionally composes and posts under the parent's identity.

Two fixes landed in v6.2:

1. **`trio-sentinel` subagent template** — `tools: Bash` only. Sentinels launched with `subagent_type="trio-sentinel"` have zero MCP tools mounted in their environment. They cannot call `nth_send`, `nth_poll`, or any other trio RPC. The capability to impersonate is gone at the process layer.

2. **Session token system** — `sessions` table, `session_token` bearer capability, per-session watermark in `sessions.last_read`, `messages.author_session` provenance, `claimed_by_session` + `lease_expires_at` for task leases, role-based send rejection (`read_only` sessions can't post).

Fix #1 alone prevents the observed bug entirely. A sentinel with no MCP tools cannot post. Cannot poll and desync the watermark either, because it can't poll. The watermark desync described in the bug report is specifically a consequence of rogue `nth_poll` calls advancing `members.last_read` — with fix #1, those calls don't happen.

## What session tokens add that the perms fix doesn't

- **Task leases** — if the claiming session dies, its tasks auto-release after the lease expires. This is a reliability feature, not a security feature. Pre-v6.2, stuck claims required `nth_cull` or manual DB intervention. Genuine improvement, orthogonal to the impersonation bug.
- **Retraction authorship** — `nth_retract` checks the caller's `session_token` against `messages.author_session`. Only the session that posted can retract. Without a session_token system, retract authorization falls back to member_id match, which is bearer-on-public-identifier — weak, matches the rest of pre-v6.2.
- **Defense-in-depth against broader threats** — any process on the machine that reads the MCP connect response could impersonate (not just sentinel sub-agents). Future agent types, inter-process context leaks, a user accidentally copying their connect response into a peer's channel. Session tokens raise the bar by requiring a secret on every mutation.

## The honest framing

Sauron and Aragorn pushed the session-token design during the v6.2 council brainstorm assuming a broader threat model than the observed incident required. The perms fix (#1) is the load-bearing defense. Session tokens (#2) happen to bundle two real wins (leases + retract provenance) with a pile of bearer-capability machinery that's overkill for the current threat model.

Every trio user today is the same human on a local SQLite DB on a single-tenant machine. Cross-session impersonation from a malicious local process was already possible pre-v6.2 (member_id-bearer everywhere) and is still possible in the legacy paths we didn't close. The session-token system closes a hole that was open in the rest of the system — so we have a half-sealed bucket.

## v7 simplification proposal

Drop bearer semantics from session identifiers. Keep the features.

**Schema change:**
- Rename `session_token` → `session_id` in the `sessions` table.
- `session_id` is a server-assigned identifier (e.g., monotonic row-id or UUID). Not a secret.
- Returned in `nth_connect` response as `session_id` (not marked sensitive).
- Passed on mutating RPCs for provenance / lease tracking.

**Behavior change:**
- Remove "don't echo the token" discipline from SKILL.md — the session_id is no longer a capability, leaking it doesn't grant impersonation.
- Drop the `role: 'primary' | 'read_only'` field. Nothing enforces role today because we accept token-less legacy calls anyway.
- Keep per-session `last_read`, `author_session`, task-lease columns. All still useful.
- Remove capability validation in send/ack/retract. Session identity is stamped for provenance but not gatekept.

**Net impact:**
- ~30% less machinery in `nth_server.py` (validation paths, role checks, token-minting entropy).
- ~20 lines of SKILL.md deleted (Session token section shrinks to 3 lines: "session_id returned, pass it on mutating calls for provenance and task leases").
- `trio-sentinel` subagent template alone stays the capability defense.
- Task leases and retract auth continue working as-is — they only needed a session identifier, not a secret.

## Tradeoff — when bearer tokens come back

The simplification loses defense-in-depth against:

1. **Future multi-tenant deployments** — if nth ever runs with cross-user access (shared hub, untrusted peers on the same SQLite DB), bearer capabilities are the right shape. Not today's deployment.
2. **Broader sub-agent tool surfaces** — if we ever give some sub-agent class partial MCP access (e.g., a Sonnet triage agent that can `nth_poll` but not `nth_send`), we'd need role-based capability gating again. Also not today.
3. **Compromise of a single MCP call** — if an attacker intercepts one MCP call and learns `(channel, member_id)` but not the token, they can't impersonate. Without tokens, they can. The current local-SQLite model makes this threat hypothetical.

If any of those become real, bearer tokens are easy to re-add as an opt-in layer on top of the session_id infrastructure. The migration is additive.

## Recommendation

Not urgent. The current v6.2 code works and the bearer-token cost is mostly in prose (SKILL.md "don't leak the token" and the Session token section). When the sentinel-simplification experiment in `reports/2026-04-18-sentinel-simplification-paths.md` lands (~v7), consider bundling this simplification into the same release. Both point at the same conclusion: we built more capability machinery than the observed threat model needs, and the real defense is the sentinel-doesn't-have-tools fix.

## References

- v6.2 council brainstorm: `reviews/2026-04-17-v6.2-council-brainstorm.md`
- v6.2 Aragorn review: `reviews/2026-04-17-v6.2-aragorn-security-review.md`
- v6.2 release report: `reports/2026-04-17-v6.2-release.md`
- Sentinel simplification paths: `reports/2026-04-18-sentinel-simplification-paths.md`
- Original bug: `bugs/2026-04-17-sentinel-agent-tool-scope.md`
