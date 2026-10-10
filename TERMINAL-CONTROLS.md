# Owner controls for existing terminal agents

The Agents page has a **Terminal sessions** section in addition to supervised
agents. The existing native compaction and approval inbox stay with the runtimes
Quartet supervises. External sessions are never silently taken over or resumed by
a second runtime process.

Tap an agent’s message avatar or name to open its controls as the owner.
Bindings use the exact Quartet member ID, never a display-name guess. Multiple
paired sessions show a chooser; unpaired sessions explain that pairing is needed.

A paired Linux host bridge provides tmux snapshots and explicit owner actions.
The owner opens **Terminal controls**, refreshes the snapshot, and can:

- Compact: literal `/compact`, pause 200 ms, Enter, pause 200 ms, Enter.
- Interrupt: Ctrl+C.
- Answer a visible question or send a slash command: one-line text, 0–2 Enter
  presses, with 100–1000 ms pauses (default 200 ms).

A command result of **sent** means tmux received the keys, not that the agent
finished compaction or accepted a prompt. Refresh to see the result. For external
sessions the page shows the current screen; it does not pretend to parse terminal
text into a structured approval. Native managed approvals remain in the existing
Attention inbox. There is no automatic approval or replay after a lost response.

## Pairing

Requires an existing Linux tmux installation and the agent running as `claude` or
`codex` in the foreground of the chosen pane. No dependency or model installation.
Use `tmux list-panes -a -F '#{pane_id} #{pane_current_command}'` locally to select
an exact pane. Pairing binds its foreground PID and kernel process start time;
replacing the process requires an explicit new binding.

Run on the agent's host, under the tmux-owning user:

```sh
python server/nth_terminal.py pair --host workstation --url https://hub.example:8765 \
  --hub-file /private/terminal-hosts.json --spoke-file /private/terminal-bridge.json \
  --member-id worker-id --binding worker --name Worker --provider claude --pane %3
```

Both files are created exclusively with mode 0600. The command never prints the
credential. Transfer `terminal-hosts.json` to the hub's NTH_HOME directory (or set
`NTH_TERMINAL_HOSTS` to its path). Transfer only that hash-bearing file to the hub;
the raw credential stays on the spoke. Existing files are never overwritten by
pairing. For several hosts, the hub file's `hosts` list holds each pairing's entry.

Add another agent or deliberately rebind a replaced process:

```sh
python server/nth_terminal.py bind --config /private/terminal-bridge.json \
  --member-id reviewer-id --binding reviewer --name Reviewer --provider codex --pane %4
python server/nth_terminal.py run --config /private/terminal-bridge.json
```

The installed launcher also exposes these as `trio terminal ...`. A bridge loads
its explicit configuration at startup; restart it after changing bindings. An
optional tmux socket is specified with `pair --socket /private/tmux-socket`.
Stop the bridge to stop accepting work. Remove its hub pairing to revoke access.
The bridge uses verified HTTPS, refuses redirects, and sends no credentials to
other URLs. No SSH access is granted to the hub.

## Authorization and outcomes

Only the existing trusted owner identity gate can read snapshots or enqueue input.
A bridge credential can report and collect commands for its own host only; it
cannot use the owner controls. Agent-facing MCP tools cannot enqueue commands.
CSRF checks apply to the browser routes.

The snapshot must be fresh (12 seconds), and the process must still match when
input starts. Compact/reply also require a matching screen. Interrupt permits
changing output so it can stop a streaming agent. The process is checked before each keystroke group.
These are best-effort process checks across tmux calls, not a kernel-atomic lock
on the terminal; use a dedicated pane for the bound agent. Terminal screens and
keystrokes are untrusted content, never interpreted as shell commands by the
bridge. Input is single-line literal text, and responses render with textContent.

Commands are consumed transactionally before delivery, so a lost delivery is
**uncertain**, not retried. Only one action can be pending per session. An audit
records the owner, action, timestamps, and outcome; completed input text is
removed. Latest snapshots live only in the hub's private database and are not
posted to channels. Stale snapshots are hidden from the UI. Do not bind a pane
whose screen you do not want the workspace owner to view.

## Deployment and rollback

Deploy `nth_terminal.py`, `nth_web.py`, `web/js/30-agents.js`, and
`web/css/30-workspace.css` to the hub after backing up the current files. Restart
only the web service. Deploy the module (and optionally updated `nth_cli.py`) to
the spoke; create pairing files and run the bridge under the tmux owner.
No live session receives input merely because it is paired.

Rollback: stop the bridge, restore the backed-up web files, and restart the web
service. Existing channels, identities, agent processes and native approvals are
unchanged. The two terminal_* SQLite tables may remain for audit purposes.

## Account usage windows

The owner panel shows the provider's available quota windows, used/remaining
percentages, reset times, and reading age. These are shared account limits, not
per-agent allowances. Managed agents read the hub's existing provider usage cache without starting
a CLI or refreshing through a model.
Paired terminals use only explicitly selected local data, never the hub account:
add `--usage-file /private/path --usage-format claude-statusline` or
`--usage-format codex-session` when pairing or binding. A Codex source must be
that session's JSONL file; a Claude source is its account's statusline cache.

Only whitelisted quota scalars cross the bridge. No credentials, account IDs,
transcript text or cache paths are sent as usage data. Missing windows are
unavailable, not zero. Old or unknown-age readings are marked cached. A Claude
cache file's modification time is not treated as the quota's update timestamp.
The bridge does not invoke a model or send `/usage` into a live terminal to refresh.
