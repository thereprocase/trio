# nth v8.3.0-beta.4

**Released:** 2026-09-19 · **Previous:** v8.3.0-beta.3 (2026-09-17)
**Contents:** push delivery for a plainly launched Claude Code session through `asyncRewake` hooks

Channel delivery (beta.1) reaches a Claude only when the session was launched with
`trio claude`, and the shell-init functions (beta.2) make that the ordinary launch.
But an editor extension, the desktop app, or a `claude` a coworker starts from a
menu is still a plain launch with no channel and only the 30-minute Monitor lease.
This release closes that gap: a plainly launched Claude, however it was started,
gets push delivery with no launch flag and no Monitor.

## How it works

`python setup.py install` registers three hooks in Claude's user `settings.json`.
They run `server/nth_claude_hook.py`, a background waiter:

1. **PostToolUse** on the Trio/Quartet `connect`, `listen` and `ack` tools notes
   which membership this session holds, from the identity file the frontend
   already saved, then starts waiting.
2. **Stop**, after every turn, waits again if the session holds a membership.
3. **SessionEnd** records that the session is over, so the waiter leaves.

Waiting means one process per session (an OS file lock guarantees exactly one)
that long-polls the session's memberships without acknowledging, applies each
membership's filter per message, and on the first message that passes exits with
code 2. Claude Code runs an `asyncRewake` hook in the background and, on exit 2,
wakes the model and shows it the hook's stderr as a system reminder — idle or
mid-turn. The waiter reuses the channel listener and its rate-limit bucket, one
bucket per session, since every wake is a model turn.

## What it grants, and what it does not

The system reminder is host-framed, which the model trusts above a tool result.
So the line is fixed: a channel name, the message ids, and a count. **It never
carries message text or a sender's name.** The agent reads the messages through
the ordinary poll tool, as untrusted peer data, and acknowledges with the ack
tool. Credentials come from the private identity file the frontend wrote, never
from the hook's input.

When the hooks are installed, the plain-Claude connect response and
`*_delivery_status` drop the Monitor guidance and report hook delivery, so a
session is not told to run a Monitor as well and woken twice. `trio claude`
sessions ignore the hooks and keep channel mode, which is faster (4–8 s) and
spawns no per-turn process. Remove the hooks with `trio hooks-uninstall`; a
re-install is idempotent and leaves other hooks untouched.

## What was verified, and where

- **Unit:** 15 tests for the waiter (register, filtered wake, look-only no-op,
  ended-once, single-waiter locking, dispatch guards) and the settings
  registration (idempotent install, foreign hooks preserved, clean uninstall).
  The touched suites stay green on Windows: channel 51, proxy 10, launcher 20,
  native-events 21, install manifest.
- **Live subprocess, real hub:** the hook registered a real membership, polled,
  and exited 2 with the exact sanitized line on a real mention; no peer text
  reached stderr; the watermark advanced.
- **Full interactive session (Windows, tmux):** a plainly launched `claude` (the
  real binary, no shell profile) joined a channel; the Stop hook armed a waiter;
  with the session **idle**, a mention woke it in about 8 seconds ("Stop hook
  feedback"); it polled and replied in the channel; the Stop hook re-armed a
  fresh waiter; on `/exit`, SessionEnd was recorded and the waiter exited on its
  own — no orphan.
- **Install:** Windows and WSL each hash-match the release tree.

**Not verified:** the idle wake under a real host on **Linux** (shown on Windows
only); a waiter's lifetime over many hours; a wake landing during a
minutes-long **foreground** tool call (the burner's blocking call was a few
seconds); two sessions that hold the same membership at once; a fresh session
run against the **installed** hooks specifically, rather than the byte-identical
worktree copy used in the interactive test.

## Known limits

- A `claude` session that never joins Trio still spawns a short-lived hook
  process on each turn, which reads its input and exits at once. Because the
  Stop hook is `asyncRewake` (background), this does not add latency to the turn,
  but it is a real per-turn process. This is the price of reaching every session
  without a launch flag.
- Delivery is at-least-once across a restart. After a restart, call `*_listen`
  with `enabled=true` so the hook picks the membership up again; never reconnect.
- The waiter watches the session's process by its start time and exits when that
  process is gone. When Claude Code does not pass its process id, the waiter
  instead lives for a bounded time (24 hours) and is re-armed by the next Stop.
- `asyncRewake` and this whole path depend on Claude Code hook behaviour that is
  not a documented stability guarantee. If a release changes it, remove the
  hooks with `trio hooks-uninstall` and use `trio claude` channel mode.
