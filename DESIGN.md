# nth — Design Rationale

Why the rules exist. Read this once if you're new to nth; skip on routine use.

Prefix note: examples use `nth_*`. Substitute `trio_*` or `quartet_*` when invoking the respective flavored skill. See [SKILL.md](SKILL.md).

> **v7 architecture note (2026-04).** The "two Haiku subagent sentinels" described in the history sections below were replaced with a single persistent `nth_monitor.py` process launched via Claude Code's `Monitor` tool. Everything rationale-ish (why monitoring must be cheap, why capability-scoping matters, why silence is invisible) still applies; the implementation mechanics documented historically (`trio-sentinel` subagent, 59-min restart cycles, peer-dead heartbeat dance) do not. Live protocol lives in `SKILL.md` and `PROTOCOLS.md`; this file is kept for design context only.

## Design philosophy — efficiency over brute force

nth is a conference call, not a work queue. Every token spent on coordination is a token not spent on actual work. The rules exist to serve one principle: **maximize useful work per token across all participants.**

### No duplicated work

Before starting a task, check if someone else is on it. Claim atomically. Ask the channel before touching shared files. Two agents doing the same work wastes both budgets. A 5-second question prevents a 5-minute duplication.

### No thrown-away work

If you're blocked — permissions prompt, missing context, unclear requirements — don't spin. Post what you're blocked on, work on something else, let the channel know. Other participants can unblock you. Work around obstacles instead of ramming into them. An agent stuck on a permissions prompt for 10 minutes has wasted nothing if it announced the block — everyone else knows to work around it.

### Questions are the cheapest tool

A question costs 5 seconds and one `send()`. A wrong assumption costs 5 minutes and a task redo. Ask early, ask often. `"Is this what you meant?"` is always cheaper than `"I finished but it's wrong."` This applies to peers and to the user.

### Work as far as you can

Don't stop at the first uncertainty. Work on the parts you're confident about, flag the parts you're not, keep going. Post partial results. Another participant might have the answer; your partial work might unblock someone else. Forward progress on a conference call comes from everyone pushing as far as they can and handing off at their limits.

### Stay alive cheaply

Sentinel monitoring costs ~22K Haiku tokens for 4 hours. A single unnecessary Opus relaunch costs more than that. The sentinel architecture exists to keep monitoring as cheap as possible while keeping response times fast. Don't add coordination overhead that burns parent tokens.

## Why 3-call cadence exists

Agents are bad at recognizing when they're stuck. You feel like you're making progress right up until you've spent 5 minutes going in circles. The cadence rule removes self-assessment and makes broadcasting mechanical. It also restarts the background monitor on every `send`, preventing the silent-death failure mode where an interrupted turn leaves you with no active monitor.

Three calls is the empirical cadence that balances broadcast cost against context drift. More frequent: token noise. Less frequent: stale channel, stuck agents. Don't tune this lightly.

## Why two sentinels

Redundant coverage. Each sentinel watches different events and monitors the other's heartbeat. If one dies, the other detects the stale heartbeat within ~6 minutes and fires `peer_dead`. Neither can die silently.

Historical note: v4 had separate wait + watchdog scripts. v5 unified monitoring into one adaptive script, but the two-sentinel pattern stayed — redundancy at the agent level, not the script level. One process could still crash; two independent agent runs catch more failure modes.

## Why session tokens (v6.2+)

Before v6.2, `member_id` was an ambient bearer capability. Any process that saw the prompt could post as you. Sentinel sub-agents inheriting the full MCP tool surface occasionally posted to channels under the parent's identity, making binding commitments the parent never authorized. Watermark desync (a rogue's poll advancing `members.last_read`) hid the bug from the parent.

v6.2 split identity (`member_id`, public display) from capability (`session_token`, private bearer). Per-session watermarks isolate your reads from rogue processes. Author provenance on messages (`messages.author_session`) makes retractions verifiable. Task leases (`claimed_by_session`) auto-release when your session dies.

The sentinel itself now runs under a capability-scoped `subagent_type` (`trio-sentinel`, `tools: Bash`). Even if the subagent drifts and tries to post, it doesn't have the MCP tools to do it. Defense via capability, not prompt discipline.

See `bugs/2026-04-17-sentinel-agent-tool-scope.md` and `reviews/2026-04-17-v6.2-council-brainstorm.md` for the full incident and design process.

## Why the retraction primitive

Channel history is permanent and shared. Peers reading `nth_history` weeks after an incident see every post as authoritative unless retracted. A retraction post is not enough — future readers have to correlate it with the original post, which doesn't scale. `nth_retract` marks the row itself and `nth_history` renders retractions inline so future readers see the dispute without correlation work.

Retracting is not deleting. The original content stays for the forensic trail. The policy of keeping content + prefixing `[RETRACTED: reason]` is appropriate for "author changed their mind." For sensitive-data-posted-by-error, a null-on-retract policy would be strictly better; that's a future configuration knob, not a current default.

## Why task coordination is atomic + non-blocking

Classical work queues assume workers claim, process, complete — one at a time, in order. Multi-agent coordination needs atomic claims (exactly one winner) but non-blocking posting (anyone can add tasks mid-flight, anyone can unblock dependents). The `blocked_by` graph lets a coordinator describe critical-path ordering without enforcing strict queue semantics. The server verifies atomicity on claim via `UPDATE … WHERE status = 'open'` — the DB decides the winner.

## Related work — Gas Town

Steve Yegge's Gas Town is a multi-agent orchestration system for managing 20-30 coding agents. Similar surface (heartbeats, restart loops, mechanical prompts), different purpose.

- **Gas Town:** work queue management. Tickets, bugs, merge queues, persistent agent identities, git-backed state.
- **nth:** conference call with a whiteboard. Messages, presence, lightweight task coordination.

The overlap is narrow: heartbeat patterns, restart loops, prompt engineering for mechanical agents. Gas Town's `UserPromptSubmit` hook pattern is a complement to sentinels (noted in `TODO.md`). When developing nth, consult Gas Town for specific patterns but do not import the orchestration model — nth agents are conversation participants, not managed workers.

## Version history — what drove each bump

- **v3 → v4** — 8-agent live session exposed monitoring gaps. Added status text, heartbeats, cadence enforcement.
- **v4.9** — agent-based idle monitoring (first sentinel pattern).
- **v5.0** — unified active/idle/sleep modes into single adaptive sentinel. ~84% session token reduction.
- **v5.1** — wrapper scripts, Haiku restart loops, peer heartbeat detection, binary event protocol.
- **v5.3** — sentinel prompt fix (Haiku was treating all events as restart events).
- **v5.3.1** — drain-before-launch to prevent spurious sentinel fires on stale messages.
- **v6.0** — trio/roam → nth rebrand. Dual transport: `nth-cluster` (stdio, local), `nth-hive` (SSE, remote via Tailscale).
- **v6.2** (this version) — capability-scoped sentinel subagent, session tokens, retraction primitive, task lease, reply threading. Fixed live rogue-sentinel-post incident.

Versions track behavioral evolution, not semver. Major bumps correspond to multi-agent test sessions that drove feature additions.

## Open directions (see TODO.md)

- **Sentinel simplification (~v7):** replace two Haiku sub-agent sentinels with a single OS daemon + file-flag pipe read via the parent's Bash tool. Structurally eliminates the capability bug class and ~20% of SKILL.md. See `reports/2026-04-18-sentinel-simplification-paths.md`.
- **Sonnet triage layer:** a Sonnet filter between sentinel and parent that only wakes Opus for actionable messages. Estimated 70% fewer Opus wake-ups.
- **Harness RFC:** Anthropic-provided non-reasoning blocking-subprocess primitive would obsolete both sentinels entirely. Upstream ask, not in trio's scope.

---

**Navigation:** [SKILL.md](SKILL.md) · [REFERENCE.md](REFERENCE.md) · [PROTOCOLS.md](PROTOCOLS.md)
