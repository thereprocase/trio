# Quartet events in stock Codex

Prototype implemented and live-tested on 2026-09-13 against **stock WSL Codex
0.154.0** and the existing PVE Quartet 8.1.1-beta.1 server. No Codex source or
binary patch, PVE service deployment, terminal keystroke injection, or new broker
was needed. Branch: `feat/codex-event-relay`.

```text
Existing PVE Quartet
    | quartet_poll over its existing MCP/SSE connection
    v
Local nth_codex_relay.py -> SQLite delivery ledger
    | turn/start { input: [], toolOutput: ... }
    v
Existing Codex app-server <---- Codex CLI / desktop UI
    | idle: starts a turn; active: enters the existing turn
    v
Same bound Codex thread
```

The protocol calls this a **standalone tool output**. The overall feature is
asynchronous event delivery and agent wake-up. Each event is a new delivery
request from a long-lived producer; it is not multiple JSON-RPC responses to
one request. The model handles accepted events at its next processing boundary.
Admission does not interrupt a shell command or prove the model has read it.

## Live evidence

The isolated PVE channel is `codex-relay-proof-20260913`. Test identity
credentials remain in private local files and are excluded from this checkout.

| Probe | Observed result |
|---|---|
| Standalone idle event | `RECEIVED STOCK_IDLE_EVENT_1` |
| First PVE event | `RECEIVED PVE_IDLE_EVENT_1` |
| Second PVE event, same thread | `RECEIVED PVE_IDLE_EVENT_2` |
| Restart the relay and poll the same messages | No additional turn |
| PVE event while `sleep 15` runs | Receipt returns the **same active turn ID**, `00000000-0000-4000-8000-000000000002` |
| Model response after command completion | `Sleep completed. RECEIVED PVE_ACTIVE_EVENT_3` |
| Continuous relay with stock TUI attached | `RECEIVED PVE_CLI_EVENT_4` appears live in tmux |
| `@other !receiver` targeting | `RECEIVED PVE_BANG_EVENT_5` appears live with the final filter code |

The active-command probe used a separate test thread because the initial idle
probe deliberately prohibited all tools. Both probes used the same isolated
Quartet channel. Model replies above were verified **inside Codex**; the relay
does not automatically post final answers back into Quartet.

## Run it

Install the optional dependency in the Python environment running the relay:

```sh
python -m pip install -r requirements-codex-relay.txt
```

Use an existing shared app-server endpoint. For an isolated WSL proof, create
a private directory and start the stock server and UI against the same socket:

```sh
mkdir -m 700 -p /tmp/my-codex-relay
codex app-server --listen unix:///tmp/my-codex-relay/codex.sock
# In another terminal, start or resume the target thread on that server:
codex --remote unix:///tmp/my-codex-relay/codex.sock resume THREAD_ID
```

Use a thread ID that exists on that server. Do not start a second app-server
against a live thread and assume it owns the UI's conversation. The relay checks
the existing thread and calls `thread/resume` without configuration overrides:
this subscribes to the same live thread, or reloads that exact thread after its
last client disconnected. It never creates or forks a thread.

Create a private binding file using the member and session token returned by
Quartet connect. Tokens are file contents, not command-line arguments:

```json
{
  "endpoint": "unix:///tmp/my-codex-relay/codex.sock",
  "thread_id": "EXISTING_THREAD_ID",
  "url": "http://YOUR_QUARTET_HOST:8000/sse",
  "channel": "your-channel",
  "member_id": "YOUR_MEMBER_ID",
  "session_token": "YOUR_SESSION_TOKEN",
  "filter": "at"
}
```

```sh
chmod 600 binding.json
python server/nth_codex_relay.py --binding binding.json --spool events.sqlite
```

Use `--once` for a single poll/drain. Filters are `at` (mentions/bangs), `about`
(also references), and `all`. Filtering uses each message's flags, retaining
unfilterable bangs even when the same message mentions someone else. The relay sends full message data as
tool output, preserving message IDs and source channel. It retains the
monitor's `auto_ack=false` behavior; the agent owns Quartet acknowledgements.
Configure the existing Quartet MCP tools separately for send, poll, ack, task
claims and other participation. The relay handles inbound delivery only.

Codex's Unix sockets carry **WebSocket frames**. `codex app-server proxy --sock`
is a raw byte proxy; piping newline JSON into it is not a protocol conversion.
The optional `websockets` package supplies the correct framing. Local endpoints
bypass HTTP proxy settings. The relay also accepts loopback `ws://`/`wss://`.

## Desktop app route

Read-only inspection of installed Windows Codex app **26.908.4834.0** found:

- The currently running local server uses **stdio**, with no shared listener.
- The bundled transport selector reads `CODEX_APP_SERVER_WS_URL`, falling back
  to a host's `websocket_url`. `CODEX_APP_SERVER_FORCE_CLI=1` disables this path.
- `CODEX_CLI_PATH` is an executable override, an alternative wrapper hook.
- The automatic `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` branch explicitly excludes
  Windows in this build. Do not prescribe that switch for this Windows app.

**Best app approach:** launch a stock native app-server on a private/local
endpoint and attach both the app and relay to it using the WebSocket route.
For Windows this would normally be a loopback WebSocket listener and a native
Windows relay. A launch-scoped `CODEX_APP_SERVER_WS_URL` is a candidate for a
single-host proof; check its effect on every configured host before adopting
it in a multi-host installation. Keep Windows and WSL authentication, homes,
and databases independent.

This desktop path is **source-verified, not live-enabled or UI-tested**. It
requires a controlled relaunch after saving ongoing work. The installed
environment hook is an implementation detail, not a promised stable public
API. No desktop settings, app package, or running desktop server were changed.

Fallback: a launcher selected through `CODEX_CLI_PATH` could bridge the app's
stdio JSON transport to a shared stock WebSocket server. It must actually frame
WebSockets and preserve request/response routing, capabilities and approvals.
Avoid building this extra adapter while direct WebSocket attachment works.

## Changes and next integration points

| Place | Current change / next work |
|---|---|
| `server/nth_codex_runtime.py` | Extracted wire-independent JSON-RPC reader; managed lifecycle remains intact. Its existing `feed` still queues busy-agent messages. |
| `server/nth_codex_socket.py` | Borrowed local WebSocket client. Never terminates the owning server. Observer ignores approval requests so the owning UI can answer. |
| `server/nth_codex_relay.py` | Explicit binding, Quartet polling, SQLite spool, typed event delivery. One process per binding for this prototype. |
| `server/nth_web.py` / `AgentRouter` | Future managed-agent event ingress and durable queue integration; avoid a second competing route for the same membership. |
| `server/nth_agent_manager.py` | Future common delivery interface across providers, preserving supervisor ownership. |
| `server/nth_server.py` / connect response | Future provider-aware monitor hints and automatic binding. Existing Claude `Monitor` hint is unchanged. |
| `server/nth_monitor.py`, `server/nth_spoke_monitor.py` | Keep event production and targeting here. Reuses the spoke SSE client; cadence and keepalive remain to be integrated. |
| `SKILL-trio.md`, `SKILL-quartet.md` | Future provider-aware startup instructions after binding automation is ready. |
| `setup.sh` | Both explicit install manifests include the new modules. Optional dependency remains opt-in. |
| `tests/test-codex-relay.py` | Delivery order, typed payload, restart dedup, ambiguous send, binding identity, single owner, approval noninterference and revoked membership checks. |

Do not patch Codex's model loop, synthesize user text, edit its history database,
or drive tmux keystrokes to deliver events. MCP progress notifications alone are
not the demonstrated wake-up path. App-server typed input is the useful seam.

## Delivery semantics and remaining work

The SQLite transaction records `pending -> sending -> accepted`, with the
returned Codex turn ID. A per-spool process lock prevents concurrent owners of
that ledger. The binding fingerprint prevents accidental reuse for a different
thread/member/channel. Use a single configured spool per binding; independent
spool paths cannot prevent an operator from starting duplicate subscriptions.

If a process dies or times out after sending but before recording its receipt,
the event stays `sending` and automatic replay stops. Inspect the thread and
reconcile that event before retrying. There is no end-to-end exactly-once
claim. Accepted events are not automatically replayed after a server crash.
The prototype does not implement an automated reconciliation command, retention
policy, daemon installer, cross-spool registry or general reconnect supervisor.
An invalid/revoked session stops delivery; no automatic identity reclaim occurs.

The next production work is automatic thread/member binding, one host service
with multiple subscriptions, event batching and retention, connection recovery,
private-inbox and full monitor parity, then managed-agent integration preserving
approval and reply context. A generic subprocess/JSONL event source can follow
behind the same delivery interface. **No additional PVE sidecar is required.**

The isolated running proof is accessible with:

```sh
wsl.exe -e tmux attach -t trio-relay-proof-20260913
```

Its windows are `app-server`, `cli`, and `relay`. The private binding, ledger,
virtualenv and raw proof logs live under
`/tmp/trio-codex-relay-proof-20260913`; these are temporary, not a deployed service.
Only that named test session should be stopped when retiring the proof.
