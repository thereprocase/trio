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

Check `trio_delivery_status` / `quartet_delivery_status` using the returned
channel, member ID and session token. `listening` is ready; `starting` may need
one brief recheck. `not_attached` means this Codex endpoint is not watched:
use `trio attach --endpoint LOCAL_ENDPOINT`, or the Trio launcher. Do not keep
polling in a model turn to compensate for missing delivery.

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

If delivery reports `reconnecting`, the local service is backing off. If it
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
