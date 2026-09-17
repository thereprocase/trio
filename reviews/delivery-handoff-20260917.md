# Delivery work handoff — 2026-09-17

This is the consolidated engineering record after the Windows/WSL launcher work.
It distinguishes repository tests, direct live observations and independent reviewer
receipts. Private transcripts, credentials, machine paths and raw session identities
remain outside the repository. Historical release notes describe their release-time
state; this report supplies the later evidence.

**Release follow-up:** `v8.3.0-beta.3` now publishes the startup fix with a distinct
version. The pre-release install/test receipts below retain their original commit
scope; see [beta.3 notes](../RELEASE-v8.3.0-beta.3.md).

## Published and installed

- Beta.1: PR #55 introduced Claude channel delivery and Codex readiness checks.
- Beta.2: PR #56 fixed listener/back-check findings; PR #57 added shell functions
  and hardened both launchers. Tag `v8.3.0-beta.2` points to `571e805`.
- PR #58: `dbedc23` serializes Codex startup, publishes its record atomically and
  preserves plain CLI fallback on runtime, OS and SQLite errors. It also prevents
  a local registration error from starting a replacement for a healthy server.
- PR #59: `7656390` adds the README delivery/Monitor explanation; no runtime change.
- Windows and WSL installations were compared against `dbedc23`: **90 server
  files and 20 skill documents match on each**, zero differences. An independent
  Windows reviewer additionally verified all 90 server files and CLI versions.
- PowerShell 7 and Bash profile functions point at the installed launchers;
  profile backups were retained. New terminal sessions load these functions.
- `NTH_VERSION` still says beta.2. Identify this follow-up by commit/hash; the
  beta.2 tag does not include PR #58. This was the state at handoff; beta.3 was subsequently published as noted above.

## What was verified

| Check | Evidence and boundary |
| --- | --- |
| Codex concurrent cold start | Real native binary on Windows and WSL: two independent launchers succeed with one server. Windows beta.2 baseline created two servers/endpoints. Event-service startup was stubbed in this concurrency probe; it proves app-server startup, not idle model delivery. |
| Startup regressions | `tests/test-codex-startup.py`: four cases pass on both OSes, covering concurrency, release after process death, failed atomic replacement, and registration failure on a healthy server. |
| Windows native startup | Independent native-executable and npm-shim startup, WebSocket initialize and loaded-thread query passed in isolated homes; test processes exited. No model calls. |
| Dispatch and fallback | `exec`/`e`, `apply`/`a`, both explicit `--remote` forms, missing saved binaries, and local/project Claude MCP shadowing were checked. Refused channel grants preserve ordinary Claude launch and remove the channel environment. |
| Final Windows tests | Startup 4, native events 20, Claude launcher 20 (one POSIX-only skip) and installer manifest pass. Earlier beta.2 independent suite: 104 passes, one POSIX-only skip. |
| Final WSL Claude idle delivery | Real Claude Code 2.1.274, launched through installed `trio claude` after `dbedc23` installation: local reply in **3.17 s**, remote Quartet reply in **3.75 s**. Both events acknowledged; no Monitor or model polling loop. Existing identities reused on resume. Test listeners stopped and CLI exited. |
| Earlier WSL Claude evidence | The beta.1 host separately passed local 7.31 s and remote 6.06 s idle reply/ack tests. These are historical measurements, not measurements of beta.2. |
| Windows Claude channels | Beta.1 reviewer evidence covers local/remote idle reply/ack, mid-turn arrival and restart recovery. A fresh interactive Windows **shell function → development-channel confirmation → ready → idle reply/ack** on the final install remains unmeasured. Version/mcp/piped-input function checks are not that acceptance test. |
| Current WSL Codex conversation | Relaunch through Trio resolved its missing owning endpoint. Native incoming Quartet events and acknowledgments were observed; readiness reports true. An unrelated running app-server could not attach the original plain CLI session. |
| Broader WSL suite | 72 passed, 1 failed, 43 skipped (Node unavailable and excluded soak tests). The unchanged supervisor shutdown-state assertion failed in the suite; baseline and current isolated reruns passed. Earlier reviewers also reproduced intermittency on unchanged code. Do not describe the full suite as green. |

Run regression scripts directly using the installed Python environment (hyphenated
filenames are not importable unittest-discovery modules). The broader runner is
`PY=<installed-python> bash tests/run-all.sh`. Real-binary probe outputs and raw
review receipts are retained in private local evidence bundles; the synthetic
startup regression is checked into the repository.

## Lessons that affect implementation

- Membership is not readiness. A successful join or poll cannot establish that
  an idle agent will receive a message. Preserve identity, inspect the owning
  endpoint, and require `ready: true`; do not create a second server for an
  already-owned thread. Manual polling cannot wake an idle session.
- Trio may fail to add automatic delivery without preventing the real CLI from
  starting. Preserve arguments, input and exit status, and explicitly report
  the loss of delivery. Check the MCP registration the host actually selects,
  including local/project shadows, rather than checking user configuration alone.
- Recheck the saved Codex endpoint **after** acquiring the cross-process startup
  lease. A healthy server's registration failure must not trigger replacement.
- Monitor's 30-minute lifetime and expiry wake invalidate the old permanent-watch
  assumption. Observed locally on Claude 2.1.274; upstream issue #94393 also
  reports 2.1.270. Do not assert the first affected release from these observations.
- Git for Windows 2.55 mintty tests found ConPTY enabled by default (`isatty`
  true). With `MSYS=disable_pcon`, native Python sees named `msys-*-pty*` pipes;
  launcher terminal detection handles both. This is version-specific evidence.
- An alias that sets `CLAUDE_CONFIG_DIR` to a configuration without Trio servers
  correctly gets the explicit no-channel fallback. Install/register in the
  selected configuration before expecting channel delivery.
- Cross-shell commands are fragile: a reviewer observed Git Bash → `wsl.exe
  bash -lc` losing variables assigned in the command. Use script files for these
  probes. Windows PowerShell 5.1 also treats redirected native stderr differently
  from PowerShell 7; the final installer was run with a Python capture driver.
- A reviewer observed auto-mode refusing to let one Claude accept another's
  workspace-trust prompt through tmux. Record a blocked experiment honestly;
  do not count launching a process as a completed interactive acceptance test.

## Experimental Claude hook path — not shipped

Independent Windows tests on Claude Code 2.1.274 found:

- A settings command hook with `asyncRewake: true` and exit code 2 woke an idle
  plain Claude session twice; a Stop hook re-armed it without model intervention.
- Mid-turn exit delivered feedback at the next tool-call boundary, without
  interrupting the command. The actual running command was a five-second sleep;
  a minutes-long foreground command remains untested.
- The wake re-fired `UserPromptSubmit`. Arming must be idempotent.
- A separate background Bash task survived 45 minutes and exited successfully.
  This does not establish the lifetime or cleanup behavior of an async hook.

Design constraints from review, still to implement and test:

1. Hook stderr is presented as a host system reminder. Emit only a fixed notice
   with sanitized channel metadata and integer message IDs/counts; **never peer
   message text**. Retrieve peer content through the ordinary untrusted poll tool.
2. Read credentials from private identity files, never from hook input. Bind the
   host's session to the correct membership without trusting peer-supplied fields.
3. Arm after successful connect/listen; re-arm from Stop. Maintain one waiter per
   session, idempotently, and stand down when `TRIO_CLAUDE_CHANNEL=1` already gives
   the session channel delivery. Preserve the listener's rate limit/token bucket.
4. Qualify Linux, hours-long lifetime, session-exit orphan cleanup, resume,
   clear/compaction, concurrent hooks, never-joined sessions and long foreground
   calls before adopting it. A candidate that might cover IDE/desktop launches
   is not proof those launch paths work.

## Remaining work and cleanup boundaries

See [TODO.md](../TODO.md#delivery-wrap-up-2026-09-17) for actionable follow-ups.
No hook implementation was started by the release author; their source trees
were reported clean. Merged branches/worktrees may remain as recovery material.

The release author reported nine older Windows Quartet-proxy processes left from
before the SSE-close fix. Some may belong to live sessions; ownership and liveness
must be checked before cleanup. An old test channel remains open. This handoff
neither authorizes killing shared processes nor ending channels. Test processes
owned by the startup and final WSL delivery probes were cleaned up.
