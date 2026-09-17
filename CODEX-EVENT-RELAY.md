# Native Trio event delivery

Trio 8.2.0-beta.1 supplies local supervision and delivery for stock Codex.
Quartet remains the remote channel authority. No Codex source/binary patch,
history database editing, terminal typing, new broker or PVE sidecar is needed.

```text
Local Trio database / existing Quartet MCP hub
                   |
          nth_event_service.py
          registry + per-binding SQLite ledger
                   | standalone tool output
          owning stock Codex app-server <--- CLI or app
                   |
             exact joined thread
```

The protocol calls this a **standalone tool output**: `turn/start` with
`input: []` and `toolOutput: {name, namespace: null, output}`. Each event is a
new JSON-RPC request from a long-lived producer, not repeated responses to one
request. An idle thread starts a turn. An active thread consumes accepted input
at its next model-step boundary, typically after the current tool/batch finishes.
Acceptance neither interrupts that tool nor proves the model has read the event.

## Installation and launch

Use `python setup.py install --quartet-url http://YOUR_HUB:8000/sse`.
Select providers with `--clients claude,codex` and a stock binary with
`--codex-binary PATH`. See README for platform commands. The installer preserves
unrelated registrations/settings and makes timestamped backups before replacing
files. It installs both skills and all their companion instructions.

`trio codex` starts/reuses the local stock server and launches the stock TUI
with `--remote`. POSIX uses a Unix WebSocket; Windows uses loopback WebSocket.
`trio desktop --app PATH --isolated` launches the Windows app in a separate UI
profile with `CODEX_APP_SERVER_WS_URL` scoped to that process. This installed
app hook is version-sensitive, not a stable public API. Other existing app
windows continue using their original connection. Windows and WSL retain
separate Codex homes, authentication and databases.

For an already exposed **owning** endpoint use `trio attach --endpoint ...`.
Never start a second server to manipulate an active thread owned elsewhere.
Existing stdio-only sessions need a new launch through Trio. `trio bind` is
explicit recovery from the private `identity_file` returned by connect and the
known thread ID; normal operation needs neither manual binding nor thread IDs.

## Automatic membership and controls

The service watches only locally configured endpoints. It subscribes to their
loaded threads and accepts only successful `nth-trio/trio_connect` or
`nth-qweb/quartet_connect` MCP completions. Connect history catches joins that
finish before subscription. Remote content cannot choose an endpoint or source
URL. Replay of the same successful connect preserves the current filter and
stopped-listener state. No agent tokens appear in status output or process argv.

Use `*_delivery_status` and `*_listen` with your membership credentials to
inspect, change filters or stop a subscription. `trio status` reports all local
listeners. `all`, `about` and `at` use per-message targeting; bangs pass every
filter, including `@someone_else !me`. Delivery never acknowledges messages on
the agent's behalf: acknowledge only through the highest message ID processed.

One local service supervises multiple bindings and retries transport failures
with bounded backoff. Successful receipts survive reconnects and restarts.
Revoked membership or ended channels stop the binding without auto-reclaim.
Service helpers detach from the launcher, so closing the TUI is not a request
to stop listening: use `*_listen(enabled=false)` before leaving a subscription.
The service resumes enabled bindings when next started after a reboot; the
installer does not add system boot jobs.

## Delivery ledger and recovery

State lives under `NTH_HOME/events`, default `~/.claude/nth/events`.
`registry.sqlite` holds private membership bindings; one ledger per binding
records `pending -> sending -> accepted` and the returned turn ID. Each ledger
has one process owner. Filter changes retain receipt history. An observer
leaves approval requests to the owning UI and never kills its borrowed server.

If a send loses its response, the listener reports `attention` with
`unconfirmed_delivery` and retains `sending`. Automatic replay stops because
the server may already have accepted it. Stop the subscription, inspect that
exact thread and ledger, then reconcile the row as accepted with the observed
turn ID, or as pending only when non-delivery is established. Back up the ledger
before editing it, and explicitly re-enable the listener afterward. There is
no automated reconciliation command or end-to-end exactly-once claim. Accepted
events are not automatically replayed after a Codex crash. Retention is manual.

## Provider behavior and integration seams

| Component | Responsibility |
|---|---|
| `nth_event_service.py` | Host registry, thread observation, lifecycle and retry supervision |
| `nth_codex_relay.py` / `nth_codex_socket.py` | Source polling, receipt ledger, typed input, borrowed transport |
| `nth_event_sources.py` | Canonical local poll/status or existing remote MCP connection |
| `nth_quartet_proxy.py` | Local stdio frontend, unmodified remote tool results, local delivery controls |
| `nth_claude_channel.py` | Claude channel listeners inside the stdio frontends, held-token completion, ack evidence |
| `nth_event_access.py` / `nth_watch.py` | Private identity persistence, provider-specific startup, readiness and recovery hints |
| `nth_codex_runtime.py` | Managed agent feeds into active turns; preserve reply audience |
| `nth_cli.py` / `setup.py` | Native launch, attach, inspection and installation |
| `AGENT-RUNTIME.md`, both skill/reference/protocol flavors | Agent workflow and acknowledgement rules |

Claude launched with `trio claude` receives the same `new_messages` payload as
a channel event from a listener inside its stdio frontend, as AGENT-RUNTIME.md
describes; that path has no receipt, so it never uses this relay's `accepted`
state. Launched plainly, Claude uses its Monitor, with its exact command
returned by connect, which preserves canonical message, cadence and keepalive
events. Codex currently receives channel messages; cadence/keepalive reminder parity
and a general subprocess/JSONL source adapter remain follow-up work. Managed
feeds do not infer a final broadcast destination after mixing audiences.

Verification is recorded in [the release record](reviews/native-events-20260913.md).
The earlier explicit-binding proof and active-turn/CLI evidence remain in
[the prototype record](reviews/codex-relay-proof-20260913.md).
