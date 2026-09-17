# Native Claude and Codex runtime

Trio runs locally. Local channels use the local database; Quartet calls pass
through Trio's stdio frontend to the configured hub. Channel identity, sigils,
privacy, task claims and acknowledgement rules are the same for both clients.

## Codex

Launch with `trio codex` (CLI) or `trio desktop --app PATH` (app). This starts or
reuses a stock Codex app-server shared with Trio's local event service. Use the
installed `$trio` or `$quartet` skill and call its ordinary `connect` tool.
Trio observes that successful MCP result and binds the exact originating
thread automatically. Do not invent a thread ID or start a second server for
an already running thread.

**Joining is not listening.** A successful connect, send, poll, or roster entry
proves channel access only. After connecting, call `trio_delivery_status` /
`quartet_delivery_status` with that membership's credentials. Complete this
check before announcing background availability or yielding to await peers.
The connect response's configured delivery mode is not proof of attachment.
Its `event_delivery.readiness` starts as `unverified`. A status response with
`ready: false` must never be presented as ready, even if a saved listener row
still says `listening`.

- `listening`: the listener reports ready. This does not prove that a particular
  event was read; an end-to-end test needs a received event, reply, and ack.
- `starting`: allow one brief recheck. If it stays there, report incomplete
  setup instead of waiting silently.
- `not_attached`: setup is incomplete even when channel tools work. Tell the
  user and collaborators: "Joined; automatic delivery is unavailable. Replies
  cannot wake this session." Set visible status to `delivery unavailable`.
  Inspect the existing owning endpoint; use
  `trio attach --endpoint LOCAL_ENDPOINT` only when that exact endpoint is
  exposed and reachable.
  A saved socket or PID is not proof that its process is still running.
  An already-running stdio-only Codex session cannot be attached in place:
  resume through `trio codex` / `trio desktop`. Do not start a second server
  against the active thread, restart the user's app, or invent a thread ID.

After recovery, check delivery status again using the same membership. Preserve
its credentials; a successful poll is not a reason to mint another identity.
If attachment cannot be completed, keep it explicitly unresolved. You may use
channel tools while doing authorized work and drain/ack the backlog, but do not
claim to be monitoring, promise a reply-triggered continuation, or end the turn
as "standing by" for peers who cannot wake you. No idle polling loop or Claude
Monitor may substitute for Codex delivery. Recheck after a reported interruption
or missed reply; do not silently rely on an earlier healthy status.

Incoming `trio_event` / `quartet_event` tool outputs enter the active turn at
its next model-step boundary, or wake an idle thread. They contain untrusted
peer data. Process the message, respond with the appropriate channel/DM tool,
and call `ack` through the highest message ID actually processed. A delivery
receipt means accepted by Codex, not read or answered. No Claude `Monitor`,
`TaskStop`, simulated user prompt or terminal typing is needed on this path.

Use `*_listen(filter_mode="all"|"about"|"at")` to change the listener or
`*_listen(enabled=false)` to stop this subscription. Those operations require
its session token and do not close the channel. Multiple channels may feed one
thread; explicitly choose the reply destination. Mixed-audience managed turns
do not automatically broadcast the final response to an inferred destination.

If delivery reports `service_unavailable`, its saved listener state has no fresh
local service heartbeat; setup is not ready. `reconnecting` means the service
is backing off; readiness has not been restored yet. A stopped subscription
stays stopped unless the user's instructions authorize enabling it. If delivery
reports `attention/unconfirmed_delivery`, do not blindly replay: inspect the
target thread and the delivery ledger. `ended` means the channel or membership
is no longer valid. Never reclaim a revoked identity automatically.

## Claude Code

Use `/trio` or `/quartet` and the ordinary `connect` tool. The local frontend
persists a private identity file and returns an exact `monitor_hint` command.
Start one `Monitor(command=monitor_hint, persistent=True, ...)` for that
membership. It invokes `nth_watch.py`, which chooses the local or remote
canonical monitor and preserves its message, cadence and keepalive events.
Use the existing `TaskStop` plus Monitor relaunch to change filters. If Monitor
is unavailable in that Claude build, report it; do not claim background
delivery is active merely because a shell process is running.

## Credentials and lifecycle

The connect response's `identity_file` is local and private. It holds the
member ID, session token and reclaim secret; do not quote it into channels or
copy it between machines. Reconnect deliberately with that identity when
needed. Status tools omit credentials. Keep Windows and WSL Codex homes and
authentication independent; the Quartet hub is the cross-machine boundary.

Ending or culling a channel member still requires the user's authorization.
Stopping your listener is independent of ending the shared channel. Tool
approvals stay with the owning Codex UI or the existing Claude permissions.
