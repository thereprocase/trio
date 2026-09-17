# Trio v8.3.0-beta.3

**Released:** 2026-09-17 · **Previous:** v8.3.0-beta.2

This release includes the Codex startup fix and the delivery documentation that
were previously available only on `main`.

## Changes

- Simultaneous `trio codex` launches now share one app-server. A cross-process
  lease serializes startup on Windows and WSL, and the endpoint is rechecked
  after acquiring the lease. The lease is released if its owner crashes.
- The saved server record is replaced atomically. A local registration failure
  no longer starts a replacement for a healthy server.
- Runtime, OS and SQLite startup errors preserve ordinary Codex launch with an
  explicit warning that push delivery is unavailable.
- The README now explains Codex delivery, Claude's native MCP channel path,
  Anthropic's Monitor lifetime change, and the available fallbacks.
- CURRENT, TODO and the consolidated handoff preserve final verification,
  experimental hook findings, and remaining work. The version now distinguishes
  this runtime from the beta.2 tag.

## Install or upgrade

From your existing clean checkout:

```sh
git fetch origin --tags
git checkout v8.3.0-beta.3
python setup.py install
```

Existing saved Quartet settings are retained. For a new installation, follow the
[README quick start](https://github.com/thereprocase/trio/blob/v8.3.0-beta.3/README.md#native-quick-start).
Launch with `trio codex` or `trio claude`; shell functions installed previously
still point at the same launcher. Restart sessions to load the new files, then
join a channel and require `*_delivery_status` to report `ready: true`.

## Verification and limits

The runtime fixes were independently tested with real concurrent Codex processes
on Windows and WSL: both launchers reused one server. Startup/native-event/Claude
launcher regressions pass. Final WSL Claude local and remote idle messages were
answered and acknowledged in 3.17 s and 3.75 s without a Monitor or polling loop.
These live tests were performed on `dbedc23`; beta.3 changes only the version
constant and documentation relative to that tested runtime.

The broader WSL suite recorded 72 passes, one intermittent failure in unchanged
supervisor shutdown code, and 43 skips (Node unavailable and excluded soak tests).
The isolated supervisor reruns passed; the full suite is not claimed fully green.

The final Windows interactive Claude shell-function idle-wake acceptance remains
open. Store updates can still invalidate a saved `codex_app` path. Native app/IDE
compatibility is separate from terminal launcher support. The experimental
`asyncRewake` hook path is **not shipped**. See the
[handoff](https://github.com/thereprocase/trio/blob/v8.3.0-beta.3/reviews/delivery-handoff-20260917.md)
and [TODO](https://github.com/thereprocase/trio/blob/v8.3.0-beta.3/TODO.md#delivery-wrap-up-2026-09-17)
for the precise evidence and remaining work.
