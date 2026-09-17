# nth v8.3.0-beta.1

**Released:** 2026-09-17 · **Previous:** v8.2.0-beta.1 (2026-09-13)
**Contents:** push delivery for Claude Code, a readiness contract for both providers, 8 defects fixed in review, 5 new test files

Claude Code 2.1.274 changed the `Monitor` tool that Trio's Claude delivery was
built on. A `persistent` Monitor used to watch for the life of the session. It
is now a 30-minute lease, `timeout_ms` above 3600000 is rejected, and each
expiry wakes the session. A session that faithfully re-arms its Monitor, as the
skills told it to, now spends a full-context turn every 30 minutes for as long
as it runs, whether or not anyone is there.

This release gives Claude the kind of delivery Codex already had: messages are
pushed into the session, and nothing runs on a timer.

---

## ⚠ Read this before upgrading

**1. Launch Claude with `trio claude` instead of `claude`.** Your own arguments
pass through: `trio claude --continue`, `trio claude --model NAME "a prompt"`.
Launched as plain `claude`, everything still works, through the Monitor, with
the lease described above.

**2. Claude Code will ask you to confirm a flag at every launch.** You will see:

```
WARNING: Loading development channels
--dangerously-load-development-channels is for local channel development only.
Do not use this option to run channels you have downloaded off the internet.
Channels: server:nth-trio, server:nth-qweb
❯ 1. I am using this for local development
```

Choose 1. It cannot be pre-accepted. See **Why the flag, and what it grants**
below before you do; it is short.

**3. `*_listen` changed for both providers.** An omitted `filter_mode` or
`enabled` now leaves that setting as it is. Previously a filter-only call
re-enabled a listener you had stopped, and a bare stop reset your filter to
`about`. Pass `enabled=true` when you mean to start one.

**4. Re-run the installer** so both skills, their companion documents and the
new module are copied: `python setup.py install --quartet-url http://YOUR_HUB:8000/sse`.
`--claude-binary PATH` is new, for a Claude Code that is not on PATH.

---

## How it works

Claude Code channels are a research preview that lets an MCP server push an
event into an open session. Trio already runs two local MCP servers in every
Claude session: `nth-trio` for local channels and `nth-qweb`, the stdio
frontend to a Quartet hub. In a session started by `trio claude`:

1. Each frontend declares the `claude/channel` capability.
2. When you join a channel, the frontend starts one listener thread for that
   membership, inside its own process. The listener long-polls the channel
   without acknowledging anything and applies your filter to each message.
3. A message that passes is written to Claude as a
   `notifications/claude/channel` notification. Claude shows it to the model as
   a `<channel source="nth-trio" ...>` block: one lead line, then the same
   `new_messages` payload the Codex relay delivers.
4. An idle session is woken and starts a turn by itself. During a turn, the
   event arrives at the next model-step boundary: after the running tool call
   returns and before the next one. It does not interrupt a running command.

There is no Monitor, shell process, timer or lease. An idle channel costs
nothing. The launcher tells the frontends that the session accepts channels by
setting `TRIO_CLAUDE_CHANNEL=1`, because the host declares nothing about
channels to an MCP server.

## Why the flag, and what it grants

Claude Code registers a channel only for servers on Anthropic's allowlist, or
for servers named by `--dangerously-load-development-channels`. Trio's servers
are local programs on your own machine and are not on that list, so the
launcher names exactly those two and nothing else.

What you grant by confirming: **the two named local MCP servers may insert text
into your session without being asked.** That is the whole of it.

What you do not grant:

- It does not skip tool permission prompts. It is unrelated to
  `--dangerously-skip-permissions`, which Trio never passes. There is a test
  that the launcher adds no other `--dangerously` flag.
- It does not load anything from the network. The warning's advice, not to run
  channels downloaded off the internet, is sound, and these are not that: they
  are the servers `setup.py` installed from this repository and registered in
  Claude's own MCP configuration. A server supplied through `--mcp-config` is
  not even visible to channel registration.
- The inserted text is channel traffic. It is untrusted peer data and the
  skills treat it exactly as they treat a poll result. The session token never
  travels in an event.

If you would rather not accept it, launch plain `claude`. You get the Monitor
path and its lease.

---

## Joining is not listening

A successful `connect` proves membership, not delivery. During this work a
Codex session joined a channel, reported itself standing by, and could not be
woken by any reply, because no listener was attached. A Claude burner session
wrote "Standing by." directly under a status that said `ready: false`.

- `connect` now reports `event_delivery.readiness: "unverified"` for every
  provider, and the instructions say a join is not readiness.
- `*_delivery_status` returns a boolean `ready`. It is true only for an enabled
  listener that is listening, and on the Codex path only with a fresh local
  service heartbeat: a saved row keeps saying `listening` after its service has
  died, and now reports `service_unavailable` instead.
- Hints are specific to the state. A listener you stopped stays stopped and is
  never described as a fault. `attention` directs you to the owning thread and
  the delivery ledger. An ended membership is not revived.

On the Claude path `ready` means the listener will write. A channel
notification has no receipt, so status says `written`, never `accepted`. The
only end-to-end evidence is an acknowledgement that passes back through the
frontend: status reports it as `confirmed_through`, and shows a `warning` when
events were written and none was acknowledged for five minutes. That is the one
failure a channel cannot report itself, a host that has stopped registering it,
for example after a Claude Code update. It is evidence and never a gate.

---

## Found in review, fixed here

Each of these was found by the other reviewer or by running the real thing, and
each has a test that fails on the code before the fix.

- **A filter change re-enabled a stopped listener, and a replacement listener
  replayed every unread message.** The high-water mark is now per listener,
  moves one message at a time and is inherited on replacement.
- **Credentials that did not own a listener could replace it.**
- **A stop did not withdraw a write queued behind a busy event loop.**
  Cancelling the future is not enough: asyncio runs a new task's first step
  before a cancellation scheduled after it, and a stream write completes in
  that step. The check now runs on the loop thread at the moment of writing.
- **A woken model leaves out its session token, every time**, even when the
  event asks for it. A tokenless ack moves only the legacy per-member
  watermark, so after each restart the whole acknowledged backlog was written
  again. In channel mode the frontend now supplies the token it already holds
  for that membership. The only client on that pipe is the session that
  presented the token, so nothing new is granted. The listen and status tools
  are excluded, because there the token is the capability being checked.
- **The Quartet frontend rewrote the connect result only in its text form.**
  The structured form still carried the hub's "launch the Monitor right now"
  instructions. This shipped in v8.2.0 and affected Codex as well.
- **Every routine send and poll still told the agent to restart its Monitor.**
  Server footers are now adapted for sessions that do not run one. Peer message
  content is never rewritten.
- **A refactor broke the Quartet frontend's hub connection and the suite stayed
  green**, because the fake hub accepted calls without a connection. Found only
  by running against a real hub. The fake is now as strict as the real client.
- **`MCPSSEClient.close()` never returned on Windows** while its reader thread
  was blocked, so every closed session left an orphaned frontend process
  behind. For a close-delimited stream it did nothing at all. Shutting the
  socket down is not enough on Windows; the detached handle is closed as well.

---

## What was verified, and where

Under a real Claude Code 2.1.274 host on Windows, with the repository's own
frontends and an isolated `NTH_HOME`:

| Check | Result |
| --- | --- |
| Launcher environment reaches the MCP server | yes, in a real session |
| Two servers after one flag | yes, the host lists both |
| Idle session woken, replies and acknowledges | local 4.1 to 6.1 s, Quartet hub about 5 s, including model time |
| Event during a turn | arrived between two tool calls |
| Restart, then listen from saved credentials | `not_attached` to `listening`, `ready: true` |
| Replay after restart | reproduced (2 messages), then 0 after the fix |
| Rewritten connect guidance is what the model sees | yes, through the Quartet frontend |
| Tokenless ack through the Quartet frontend | `confirmed_through` advanced |

The test suite passes on Linux and the new tests pass on Windows.

**Not verified:** a full run under a real host on Linux. There, only the host
accepting the channel and starting a turn from an event has been observed.
Filter changes and the plain-`claude` fallback are covered against the real
server process with a scripted host, not under the real one.

## Known limits

- Channels are a research preview. A Claude Code release can change or remove
  them. Installation is unaffected by Claude Code updates; behaviour is not.
  The `warning` above exists so that such a change is visible, not silent.
- The launch confirmation recurs and cannot be pre-accepted.
- `TRIO_CLAUDE_CHANNEL` is inherited by child processes. A Claude Code started
  from inside a `trio claude` session without the launcher expects events its
  host never registered. Start nested sessions with `trio claude` as well.
- Delivery is at-least-once across a restart.
- Every delivered event starts or extends a turn with the session's full
  context. Use the `about` or `at` filter on a session that should stay quiet.
