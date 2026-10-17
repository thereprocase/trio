# Merge review: release/v8.3.0-beta.5

- **Branch:** `release/v8.3.0-beta.5`
- **Base:** `main` at `08b667d (2026-10-16 Use desktop space for conversations and wide tables)`
- **Date:** 2026-10-17
- **Commits:** 7 (5 before review, 2 review fixes)
- **Files changed:** 15

## Summary

This branch releases 8.3.0-beta.5, which collects the 140 commits merged since beta.4. beta.4 recognised its Claude delivery hooks only by a group key that settings writers drop. When the key was gone, every plain `claude` session was told to run a Monitor. main already fixed that. Installing main on Windows exposed two installer bugs: an `os.getuid` crash, and an empty `CODEX_HOME` resolving to the current directory. The full suite also exposed a crash in the channel-pins migration and a racy tmux test. This branch fixes all four, and the review added a Windows junction guard.

## Review history

| Reviewer | Role | Findings (C / W / N) | Status |
|----------|------|----------------------|--------|
| Ent (Sonnet) | Triage | 0 / 3 / 2 | Recommended Gimli, Aragorn, Uruk-Hai |
| Gimli (Sonnet) | Cross-platform install | 0 / 0 / 5 | CLEAN |
| Aragorn (Sonnet) | Security boundary of `private_dir` | 0 / 2 / 4 | 1 warning fixed, 1 documented |
| Uruk-Hai (Haiku) | Edge-case hunt | 1 confirmed bug, not introduced by this branch | Filed as #31 |

## Key findings

### Warnings

1. **Aragorn: NTFS junctions pass `private_dir` on Windows** (`server/nth_interposer_wire.py:58`). Verified on Python 3.12.10: a junction's `lstat()` reports `S_IFDIR`, and creating one needs no privilege. The Claude hook state directory (`server/nth_claude_hook.py:169`) skipped all checks on Windows. **Fixed** in `c66960f`: both places refuse every reparse point on Windows. `tests/test-interposer-store-portable.py` creates real junctions and fails against the pre-fix code.
2. **Aragorn: a non-default `NTH_HOME` with a permissive ACL** reopens the planted-directory attack on Windows, because the owner check is skipped there. **Documented** in `AGENT-RUNTIME.md`: keep `NTH_HOME` inside the user profile. An owner-SID check is deferred.
3. **Ent: no test for the pin-seeding guard** (`server/nth_web.py:2110`). **Fixed** in `67a4fde`. The test covers a `channels` table without the column and seeding from one with it.
4. **Ent: the portable store test was a smoke test.** **Fixed** by the junction cases in `c66960f`. POSIX owner refusal stays covered by the existing interposer tests.
5. **Ent: interposer helpers silently do nothing on Windows.** Gimli classified this as intended. The interposer is Linux-only (AF_UNIX, systemd, "named pipes are deferred"), and every hook call site catches it. **Documented** in `AGENT-RUNTIME.md`.

### Bug found in existing code (not introduced here)

- **Uruk-Hai: pins diverge between the server and the dashboard.** The MCP server writes only `channels.pinned_message_id` (`server/nth_server.py:1644`, `:2455`). Once `channel_pins` exists, the dashboard's pins dialog reads only that table (`server/nth_web.py:9099`). Pins set by agents after the migration do not appear in the dialog. Settling this needs a decision on how the objective pin relates to the multi-pin list, so it is filed as a follow-up and not changed in a release branch.

### Notes

- A whitespace-only `CODEX_HOME` is still taken literally, consistently in `setup.py`, `codex_context_publisher.py` and `nth_codex_hook.codex_home()`. The behavior is unchanged from main.
- The `--codex-home` default in `codex_context_publisher.py` has no test. It is a one-line change that mirrors the tested installer change.
- Only the historical "Prior" line in `AGENTS.md` still names `8.3.0-beta.4`. `AGENTS.md` references a `CHANGELOG.md` that has never existed in the repository.

## Fix log

| Commit | Fix |
|--------|-----|
| `7f6ac34` | Interposer trust store opens on Windows (`os.getuid`) |
| `9635e52` | Empty `CODEX_HOME` treated as unset |
| `8dff6d2` | Pin seeding only when the old pin column exists |
| `c34848e` | tmux test waits for the agent banner |
| `e9429e0` | Version 8.3.0-beta.5 |
| `c66960f` | Review: refuse junctions on Windows; Windows interposer and `NTH_HOME` documented |
| `67a4fde` | Review: test the pin seeding guard |

## Verification

- Full suite on WSL, serial (`JOBS=1`) before review: 115 passed, 0 failed, 75 skipped. With `JOBS=8` one different file failed per run, and each passes alone.
- After the review fixes, serial: 113 passed, 2 failed (`test-channels-list.py`, `test-interposer-fixes6.py`), 75 skipped.
  - `test-channels-list.py` passed 5 of 5 alone.
  - `test-interposer-fixes6.py` was run 10 times each, interleaved, on unmodified `main` and on this branch. Both failed once, in the same test, `test_startup_inbox_does_not_delay_real_server_readiness` (`ConnectionRefusedError`). That is a startup-timing flake already on main, not a regression.
- Windows: `test-claude-hook.py` 35 tests OK (1 skipped). `test-interposer-store-portable.py` passes, and its junction cases fail against the pre-fix code.
- Installed from this branch on Windows 11 and WSL. Both report 8.3.0-beta.5 and `delivery_hooks_installed()` True, with one Trio group per hook event and 24 h timeouts.

## Known limitations and deferred items

- On Windows, a non-default `NTH_HOME` gets no owner check. Keep it inside the user profile.
- The interposer shadow observation does not run on Windows.
- Agent pins are not reflected in the dashboard pins dialog ([#31](https://github.com/thereprocase/trio/issues/31)).
