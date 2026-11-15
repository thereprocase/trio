# Merge review: fix/war-council-20261115

- **Branch:** `fix/war-council-20261115`
- **Base:** `main` at `3747967` (8.3.0-beta.5)
- **Date:** 2026-11-15
- **Files changed:** 39

## Summary

A full-codebase review of 8.3.0-beta.5 by eleven reviewers found 2 critical and 27 warning findings, most of them in the tmux notice sink, the interposer outbox, the hub deploy script and the phone message layout. This branch fixes them, adds a command that registers a tmux pane for notice delivery, moves the hub's device-to-login mapping from a local patch into `nth_web.py`, and releases the result as 8.3.0-beta.6.

## Review history

| Reviewer | Role | Findings (C / W / N) | Status |
|----------|------|----------------------|--------|
| Sauron | State and data flow | 0 / 7 / 1 | Fixed |
| Gandalf | Architecture | 0 / 3 / 4 | Fixed; adapter boundary deferred |
| Frodo | Phone and operator workflow | 0 / 2 / 1 | Fixed |
| Aragorn | Security | 0 / 2 / 1 | Fixed |
| Legolas | Performance | 0 / 2 / 0 | Fixed |
| Gimli | Build and deploy | 0 / 5 / 0 | Fixed; two deferred |
| Ents | Tests | 0 / 4 / 1 | Fixed |
| Gollum | Style and docs | 0 / 2 / 4 | Fixed |
| Uruk-Hai ×3 | Edge-case hunt, one wave | 2 / 0 / 6 | Both criticals fixed |
| Sauron (verify) | Fix diff: interposer and tmux sink | 0 / 2 / 5 | Both warnings fixed; three notes deferred |
| Aragorn (verify) | Fix diff: identity and deploy | 0 / 0 / 6 | Three notes fixed; three deferred |

## Key findings

### Critical

1. **A busy pane starved every other pane** (`server/nth_interposer_tmux.py`). The drain limited rows before grouping by session, so 32 queued notices for one busy session hid all others. **Fixed** in `59e805e`: the drain is fair across sessions.
2. **Finished answer text read as activity.** Words such as "thinking" anywhere in the last 24 screen lines held delivery back. **Fixed** in `59e805e`: the activity check is anchored to the client's live footer.

### Warnings, grouped

- **tmux sink** (`59e805e`, `bfdc8b9`): the claim and both sends run inside a lock fence; a multi-line draft defers the notice; a notice names each membership; a binding records the pane process and the client process, so a new client started in the same pane receives nothing from the old session; rebinding moves queued notices to the new pane.
- **Outbox lifecycle** (`5504298`, `bfdc8b9`, `9834d38`): queued notices follow a membership to its new owner; ending or evicting a session cancels its queue and removes its bindings; a new host process starts idle; finished rows are pruned after 7 days or 4,096 rows; a notice is cancelled once the session acknowledges its messages, read from `ack.seen` or from the hook's state file.
- **Device-owner mapping** (`c1805f1`): `NTH_TAILNET_DEVICE_OWNERS` maps a tagged device's address to a tailnet login through the existing owner/member/guest gate. The mapping requires the exact peer address and the device tag, ignores forwarded headers, and binds its cookie to the mapped address. The README states what the mapping grants.
- **Hub deploy** (`6c3109d`): `setup.sh hub-service` stages the release, checks pins, byte-compiles and imports both servers before replacing files, keeps the replaced files in `backups/<timestamp>/`, and exits 1 with `UNVERIFIED` when the health probe fails.
- **Phone layout** (`5733921`): the actions button is a 44×44 target in the gutter beside every message; the DOM test harness reads the real page skeleton.
- **Operator commands** (`2eb6476`): `trio interposer bind-tmux` registers a pane; `trio interposer status --session ID` answers on a machine whose full session list exceeds one reply frame.

### Bug found in existing code (not introduced here)

- `tests/test-codex-hook.py` ran `systemctl --user restart trio-interposer.service` against the developer's own user manager on every suite run. **Fixed** in `9834d38`.

## Verification

- Full suite, `bash tests/run-all.sh`: 190 passed, 0 failed, 6 skipped.
- Five test files run against `3747967` sources and against this branch: each fails on the base and passes here. `test-interposer-tmux.py` and `test-identity-device-owner.py` fail on the base at a missing function or attribute; `test-interposer-lifecycle.py` fails 16 of its tests there; `test-hub-service-deploy.py` and `test-header-phone-layout.js` fail on assertions.
- Live check on one Linux machine with a Claude Code session in a tmux pane: a notice was typed at an empty box and recorded `written`; with a draft in the box the notice stayed `queued`, the hook woke the session, and the notice was `cancelled` once the acknowledgement reached the service.

## Coverage

- Codex in a tmux pane was checked against saved screen captures only; no capture shows Codex mid-turn or at an approval dialog.
- The device-owner mapping was tested with a mocked `tailscale whois`; no sign-in from a mapped device was observed.
- No macOS or Windows run.

## Deferred

- A client that tmux reports as `node` receives no tmux notices (it is absent from the accepted program list).
- The screen is captured once before the lock fence; keys typed in the following two seconds can interleave with a notice.
- A re-attach clears the owner of sibling membership keys (unchanged from main).
- The deploy health probe skips certificate checks for any `https` URL it is given; `HUB_KEEP_BACKUPS=0` and non-numeric values are unchecked; pip runs before the staged checks and is not rolled back.
- The file swap is one `mv` per file; a crash in the middle is recovered by hand from `backups/<timestamp>/`.
- The owner login comparison is case-sensitive (unchanged from main).
- The dashboard resolves identity with one `tailscale whois` per request; phone CSS relies on `:has()`.
- The OpenCode adapter lives outside this repository; the service calls it uses are documented in the README.

## Sources

Run `trio-main-20261115T131413Z-ia1y`: reports `001`–`018`, proofs `proof-*-before.txt` and `proof-*-after.txt`.
