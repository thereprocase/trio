# Stock Codex / PVE Quartet relay proof

Date: 2026-09-13. Trio baseline `11c38ad8f1a7963d77e7fd6860ae17f50fbf3a15`.
Implementation branch: `feat/codex-event-relay`.
Runtime: unmodified WSL Codex 0.154.0, Python 3.12, websockets 15.0.1.
Hub: existing PVE Quartet reporting 8.1.1-beta.1. No hub code was deployed.
All messages were sent in the isolated `codex-relay-proof-20260913` channel.

## Observed protocol and model receipts

Identifiers below are substituted examples; repeated placeholders preserve
the observed same-thread/same-turn relationships. Raw identifiers remain local.

```text
CONNECTED trio_relay/0.154.0
AUTHENTICATED True
THREAD EXAMPLE_THREAD_1
AGENT READY
AGENT RECEIVED STOCK_IDLE_EVENT_1

CODEX_RECEIVED RECEIVED PVE_IDLE_EVENT_1
CODEX_RECEIVED RECEIVED PVE_IDLE_EVENT_2
RESTART_DEDUP_PASS 4 turns

ACTIVE_COMMAND EXAMPLE_TURN_2
message_id=101 state=accepted turn_id=EXAMPLE_TURN_2
SAME_ACTIVE_TURN_PASS EXAMPLE_TURN_2
ACTIVE_MODEL_RECEIPT Sleep completed. RECEIVED PVE_ACTIVE_EVENT_3

message_id=102 state=accepted turn_id=EXAMPLE_TURN_3
TUI: RECEIVED PVE_CLI_EVENT_4

message_id=103 state=accepted turn_id=EXAMPLE_TURN_4
TUI: RECEIVED PVE_BANG_EVENT_5
```

The active proof ran `sleep 15` in a separate thread that permitted this command.
The original idle-proof thread had expressly prohibited tools. The source was
PVE Quartet; no user-text submission or terminal typing delivered these events.
TUI capture confirmed the fourth marker appeared after attaching the stock CLI
and starting the continuously running relay.

## Issues caught during the proof

- Unix control sockets use WebSocket framing. Raw newline JSON through the
  stock byte proxy does not initialize the protocol.
- `websockets` local connection initially failed; explicit proxy bypass and
  disabled compression established the Unix connection.
- A disconnected thread was `notLoaded`. The first relay send therefore lacked
  a receipt. Its ledger stayed `sending`, as intended. Inspection showed only
  the two earlier test turns and no Quartet event; this exact test record was
  reconciled before retrying. Relay now verifies and resumes the same thread.
- Updating developer instructions when resuming the initial test did not remove
  its previous no-tools instruction. A fresh isolated thread was used for the
  sleep probe. This was test setup, not a delivery failure.
- Moving test HOME hid the existing user-site MCP dependency. Explicitly
  retaining its dependency search path while isolating state fixed the harness.

## Regression checks

Affected suites all passed: `test-codex-relay.py`,
`test-codex-app-server-restart.py`, `test-web-codex-agents.py`,
`test-agent-router-loop.py`, and `test-install-manifest.py`.
The final relay suite has **8 passing cases on WSL and Windows** after adding
per-message targeting. The Windows sandbox runner supplied a temporary home
for the existing request-log module's import-time home lookup.
A final live test used `@RelayProofSender !RelayProofReceiver`; the bang reached
the receiver and produced `RECEIVED PVE_BANG_EVENT_5` in the CLI.

The canonical runner completed with **64 passed, 2 failed, 43 skipped**. The
skips are six excluded soak scripts and 37 JavaScript tests because Node was
not on this WSL runner's PATH. No JavaScript code changed.
Both failures were independently reproduced against an archive of untouched
Trio baseline HEAD, as well as this branch:

- `test-monitor-session-revoked.py`: legacy database case reports a revocation.
- `test-request-log-api.py`: one-second shorthand timing assertion.

They were not changed as part of this relay implementation. Tests used isolated
homes and databases. Raw suite and baseline comparison logs remain in the local
proof directory; no private binding data is included here.

## Desktop evidence and limit

Installed Windows app 26.908.4834.0 uses a stdio app-server in the live process.
Read-only inspection of `.vite/build/src-CCXHtyvY.js` found a transport selector
reading `CODEX_APP_SERVER_WS_URL`, falling back to `hostConfig.websocket_url`;
`CODEX_APP_SERVER_FORCE_CLI=1` bypasses that selector. `CODEX_CLI_PATH` supplies
another executable hook. The automatic local daemon branch excludes Windows.

The app was not relaunched or repointed. Therefore the desktop route remains
source-verified rather than UI-tested. Credentials, full membership responses,
and session tokens are deliberately absent from this report.

See [the runbook](../CODEX-EVENT-RELAY.md) for topology, setup, remaining work and
delivery semantics. This is a tested inbound relay prototype, not a complete
provider migration or a production service deployment.
