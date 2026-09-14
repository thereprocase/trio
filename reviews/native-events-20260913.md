# Native event integration verification — 2026-09-13

This extends the explicit-binding prototype in `codex-relay-proof-20260913.md`
into the local Trio service, provider-aware tools, installer and launchers.

## Executed checks

- Canonical `tests/run-all.sh`: **104 passed, 0 failed, 6 skipped**. The six
  skips are the runner's named long-running soak suites. Python used the MCP
  environment and Node 24.15.0 ran every browser suite. Tests used an isolated
  home. The runner's CRLF copy was normalized outside the checkout for WSL.
- Final native integration suite: **9 cases passed on Windows and WSL**,
  including private identity files, actual MCP completion validation, fixed
  source/destination, stopped-filter recovery, latest-token history recovery,
  installer preservation, argument forwarding and managed reply scope.
- Stock WSL Codex **0.154.0**, live PVE Quartet **8.1.1-beta.1**: a real model
  called `quartet_connect`, the observer discovered its thread and membership,
  the listener became ready, and the model returned
  `RECEIVED NATIVE_AUTO_EVENT_1`. No thread ID was supplied to the listener.
- Same integrated WSL server, local Trio source: automatic binding and model
  receipt of `LOCAL_AUTO_EVENT_1` passed.
- Native Windows stock Codex **0.154.0-alpha.6.2** supplied with the app:
  automatic local binding passed and the model returned
  `WINDOWS_RECEIVED WINDOWS_NATIVE_EVENT_1`.
- Windows app **26.908.4834.0** launched with a separate `--user-data-dir`.
  Its actual packaged executable is `ChatGPT.exe`; direct execution of the
  `Codex.exe` stub was refused by Windows. The new app PID established a TCP
  connection to Trio's loopback app-server, verified separately from the two
  Python observer/listener connections. Existing app windows were retained.
  This proves launch and transport attachment, not a screenshot-based UI check.
- Earlier live prototype checks retain evidence of idle wake, repeated events,
  delivery during `sleep 15` returning the same active turn ID, restart dedup,
  and `@other !receiver` delivery visibly appearing in the stock TUI.

## Limits and operator context

The WSL Claude live model/Monitor test could not authenticate: its OAuth
session expired and refresh failed. The installer and provider-aware Monitor
command are covered by tests; do not represent the live Claude session as
verified until login is refreshed and that probe passes.

The remote test channel is `trio-native-20260913`. The receiver is an external
WSL participant, not a hub-managed agent. Its place in the PVE UI is the channel
roster, not the managed-agent fleet. The dashboard at
`http://pve.tail958a3.ts.net:8765/` returned 200; a fresh unauthenticated channel
index request was denied by the operator gate. No dashboard access policy,
hub process or deployment was changed.

Private identities, tokens, ledger files and raw tool responses remain in
local private test state outside this checkout. This record contains no
credentials. The app environment hook is version-sensitive. Codex cadence and
keepalive reminders, automatic receipt reconciliation, and retention remain
documented follow-up work.
