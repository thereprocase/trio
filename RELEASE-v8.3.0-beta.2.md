# nth v8.3.0-beta.2

**Released:** 2026-09-17 · **Previous:** v8.3.0-beta.1 (2026-09-17)
**Contents:** plain `claude` and `codex` can start through Trio; two launcher fixes; the fixes from an independent back-check of beta.1

v8.3.0-beta.1 gave Claude Code push delivery, on one condition: the session had
to be started with `trio claude`. A session started the ordinary way could not be
given a channel afterwards. Neither Claude Code nor Codex accepts pushes through
anything that was not opened at launch, and Claude Code has no setting that
opens it: the channel flag is a launch argument and nothing else.

So the fix for "I forgot to launch it the special way" is to make the ordinary
way the special way. This release lets the words `claude` and `codex` mean
`trio claude` and `trio codex` in your terminals, and makes the launchers step
aside for everything that is not an interactive session.

---

## Set it up (once per machine, per shell)

**1. Install or upgrade as before:**

```
python setup.py install --quartet-url http://YOUR_HUB:8000/sse
```

Omit `--quartet-url` for local-only use. The installer now ends by printing the
next step with the launcher's full path filled in.

**2. Add the two shell functions to your profile.**

PowerShell:

```
New-Item -ItemType Directory -Force (Split-Path $PROFILE) | Out-Null
trio shell-init powershell | Add-Content -Path $PROFILE
```

bash (for zsh use `shell-init zsh` and `~/.zshrc`):

```
trio shell-init bash >> ~/.bashrc
```

Use `Add-Content` in PowerShell, not `>>`: Windows PowerShell 5.1 appends
UTF-16 with `>>`, which corrupts a UTF-8 profile. If `trio` is not on PATH, use
the full path the installer printed.

**3. Open a new terminal.** `claude` and `codex` now start through Trio, from
any directory, with your arguments and piped input passed on.

`--clients claude` (or `codex`) prints only that one function, for a machine
where the other command should stay as it is.

Trio prints the functions and never edits a profile itself. To undo the change,
delete the two functions from the profile; the real binaries are untouched. The
functions hold absolute paths to this installation, so run `shell-init` again
after moving or reinstalling Trio.

What this looks like in the profile (paths are examples):

```
claude() { '/home/you/.claude/nth/venv/bin/python' '/home/you/.claude/skills/nth/server/nth_cli.py' claude "$@"; }
codex() { '/home/you/.claude/nth/venv/bin/python' '/home/you/.claude/skills/nth/server/nth_cli.py' codex "$@"; }
```

They call the interpreter and the launcher by path. On Windows that avoids
`trio.cmd`, through which cmd.exe parses the arguments a second time.

## What changes once it is set up, and what does not

- An interactive `claude` gets the development-channels flag naming Trio's two
  local servers. Claude Code asks you to confirm it at every launch, as in
  beta.1; what that grants is unchanged and is described in the beta.1 notes
  and in `AGENT-RUNTIME.md`. A session listens to nothing until it joins a
  channel, so a session that never uses Trio pays only that one keypress.
- Everything that opens no session reaches the real binary exactly as typed:
  `claude mcp ...`, `update`, `doctor` and the other subcommands, `-p/--print`,
  `--help`, `--version`, `--bg`, and any launch without a terminal on stdin and
  stdout, such as a script or a pipe. `TRIO_CLAUDE_CHANNEL` is removed from
  their environment and Claude's MCP registration is not checked, because
  `claude mcp` is how a broken registration gets repaired.
- An interactive `codex`, and the subcommands that Codex itself lets run
  against an app-server (`resume`, `fork`, `agents`, `queue`, `archive`,
  `delete`, `unarchive` in codex-cli 0.154.0), use Trio's shared server.
  `exec`, `login`, `mcp`, `update` and the rest reach the real binary as typed
  and no longer start that server. Options may come before a Codex subcommand,
  so the options that take a value are known to the launcher: `codex -C app`
  changes directory and is not read as `codex app`.
- The functions exist only in your interactive shells. An editor extension, the
  desktop app or a scheduled task starts the real binary and gets the Monitor
  path with its 30-minute lease, as before.

The subcommand lists are the ones Claude Code 2.1.274 and codex-cli 0.154.0
print. A subcommand added by a later release is not known to the launcher: a
new Claude subcommand would be given the flag, which Claude Code may refuse,
and a new Codex one would be given `--remote`. Run the real binary by its path
in that case, and report it.

---

## Fixed in the launchers

- **The channel flag was placed after a `--` in the middle of the arguments**,
  where Claude Code reads everything as prompt text. It now goes before the
  separator.
- **Ctrl+C could kill the session through the launcher.** On POSIX a terminal
  sends the interrupt to the launcher as well as to the program, and Python's
  `subprocess.call` kills its child when the caller is interrupted. The
  launches now run under a handler that does nothing. It is a handler and not
  "ignore", because an ignored signal would be inherited by the program. There
  is a test that delivers a real process-group interrupt; it fails with the
  handler removed.

## Found by an independent back-check of beta.1

After beta.1 was published, fresh agents that had not written any of it were
asked to check it. Each finding needs a faulty or hostile hub; no channel peer
can cause them. Each has a test that fails on the code before the fix.

- **A hub that failed every poll was reported as `listening` and ready.** The
  SSE client hands such a reply over as `{'_raw': ...}`, and the listener read
  it as an empty poll: deaf, and saying otherwise. A reply with no `event` and
  no `error` is now a failed poll, `reconnecting` with backoff. The
  `{'ended': true}` of a hub older than the `event` field is still an ending.
- **The notification size cap shrank only a message's text.** A message whose
  bulk sat in another field was written oversize. It is now replaced by a stub
  carrying its id and flags.
- **A listener that ended said nothing.** An idle agent on push delivery was
  never told that its channel had ended or its membership was refused, which
  the Monitor path did report. A listener now writes one `delivery_ended`
  event, with the reason and what to do. A stop the user asked for is not an
  ending and writes nothing.
- **That notice must not fire on a reclaim.** When a session reclaims its own
  membership, the hub revokes the old token before the new listener exists. A
  refused listener therefore reports `reconnecting` and waits three seconds to
  be replaced before it announces anything. This one was introduced by the fix
  above and caught in its review, before release.
- **The Quartet frontend imported the channel module at import time.** Had a
  release of the MCP library moved what that module needs, the frontend would
  have stopped starting for every client, Codex included. It is now loaded only
  for a session launched for channels, with the same loud fallback the local
  frontend has.

## What was verified, and where

| Check | Result |
| --- | --- |
| The printed PowerShell functions, in a real `pwsh` | arguments `two words`, `a&b`, `$HOME`, `it's` and piped input arrive unchanged |
| The printed bash functions, in a real bash (Linux) | the same |
| A process-group SIGINT sent to the real launcher (Linux) | the program handles it and exits with its own status; with the handler removed the launcher dies and kills it |
| `claude --version`, `claude mcp list` through the launcher, real Claude Code 2.1.274 | real output, no flag added |
| `codex --version`, `codex mcp list` through the launcher, real codex-cli 0.154.0 | real output, no `--remote`, no server started |
| Terminal detection in a real Git Bash (mintty) window, Windows | with a pseudo console `isatty` is true; with `MSYS=disable_pcon` it is false, the pipes are named `\msys-…-pty0-from-master-nat`, and the launcher still finds a terminal |
| Reclaim race and legacy `ended` reply | reproduced by the reviewer on the unfixed commit, gone after the fix |

Test files touched by this release pass on Windows and on Linux. The full
Linux suite on the back-check fixes ran 71 passed, 1 failed, 43 skipped; the
failure is `test-supervisor.py`, the timing-dependent test described in the
beta.1 notes, which passed in two of three runs alone and fails at the same
rate on unchanged `main`.

**Not verified:** an interactive session started through the shell functions
under a real host. The functions were run for real, and `trio claude` was
verified under a real host in beta.1, but not the two together. `--bg` is
passed through untested rather than given a flag nobody could confirm. The
interrupt handling is tested on Linux only; on Windows the change is limited
to not printing a traceback after the program exits.

## Known limits

Those of beta.1 stand, with these changes:

- "Start nested sessions with `trio claude`" still holds for a plain `claude`
  started from inside a session, because tools do not load your shell profile.
  A one-shot child started through the launcher is now safe: the variable is
  removed for it.
- A prompt that begins with `-` cannot be passed on the command line through
  the functions: PowerShell removes a bare `--` before a function sees it, and
  a leading `--` typed to the bash function is read as Trio's own separator.
  Type such a prompt inside the session.
- Options may come before a subcommand. The launcher follows Claude Code's own
  parsing: `claude --model NAME doctor` is `doctor`, and `claude --debug mcp`
  is a session with the debug filter `mcp`, because `--debug` takes the next
  word. When a launch that looks like a session finds no terminal, the
  launcher says so in one line on stderr.
- The launch confirmation still recurs and cannot be pre-accepted. Claude Code
  documents an administrator allowlist for channel plugins on Team and
  Enterprise plans that may remove it; that route is untested here.
