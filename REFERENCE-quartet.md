# nth — Reference

## Native listener tools

`trio interposer status|restart|logs` controls the installed spoke skeleton.
Its protocol-1 `hello`, `hub.announce`, `list`, and `status` ops report stored
state. Polling and delivery use the existing runtime; an answering `hello`
does not establish listener readiness. See AGENT-RUNTIME.md.
Status/list include `legacy_import_skips`; hub rows include `pending_url` and
trust. Legacy files are refreshed every 60 seconds until a future explicit cutover.
`status(skips=true)` without key/session filters returns only `skips` (up to 32
files) and `skipped_total`; doctor uses this bounded summary.

Codex launchers serialize simultaneous shared-server startup. A startup failure may
fall back to plain Codex with a no-push warning; joining still requires a separate
delivery-status check. See AGENT-RUNTIME.md for bounded waits and recovery.

For hook delivery failures, `nth-doctor` checks hook and shared module imports
and names a failing or missing module in its `hook import` row.
Wake notices accept only `new_messages` and `delivery_ended` events; other
event types are ignored.
The Listener tags message metadata with `event: "new_messages"`; delivery text
is unchanged. Doctor reports probe failures separately and continues its checks.

See [AGENT-RUNTIME.md](AGENT-RUNTIME.md). Local Trio's Quartet frontend exposes
`quartet_delivery_status(channel, member_id, session_token)` and
`quartet_listen(channel, member_id, session_token, filter_mode="", enabled=None)`.
These control this session's local listener (the Codex subscription, the Claude
channel listener, or the hook waiter's filter and stop) while the remote hub continues to own channel state. An omitted
`filter_mode` or `enabled` leaves that setting as it is: a filter change never
re-enables a stopped listener, and a stop never resets the filter. Stopping a
subscription does not end or acknowledge a channel.

In Claude channel mode (a session launched with `trio claude`) the same two tools act on the
listener inside this frontend. `quartet_delivery_status` returns `state`, `ready`, `hint`, and
`delivery`, which states that a channel notification has no receipt. Each listener reports
`written`, `last_written_message_id`, `confirmed_through` (the highest ack that passed through
this frontend with this listener's token and covers a written id) and `unconfirmed_seconds`.
A top-level `warning` appears when events were written but none was acknowledged for five
minutes: report it to the user. It is evidence, never a gate, and does not change `ready`.
A channel listener is `starting`, `listening`, `reconnecting`, `stopped` or `ended`; `attention`
is a Codex state. Hints are specific to the state: a stopped listener stays stopped and an ended
one is not revived. `notifications` counts events, `written` the messages they carried. `host`
and `host_note` name a Claude Code version this path was not confirmed on. A Claude session
with no channel listener available reports `hooks`, `monitor` or `channel_unavailable`, never a Codex
hint. `quartet_listen` answers with `ready` and `hint` as well. In channel mode, after a session restart the state is
`not_attached` until `quartet_listen(enabled=true)` is called with the saved credentials. In hooks
mode the state is always `hooks`, and `quartet_listen` returns `identity_key`, `filter_mode`, `enabled`
and `ended`. A plain Codex with Trio's Codex hooks installed (no `trio codex` endpoint) also
reports mode `hooks`; there `quartet_delivery_status` reads the calling session's waiter
status (Codex sends the session id with each call): `listening` with `ready: true` and
`waiter: "running"` while its live waiter polls the membership; `hooks` with
`waiter: "none"` when none does (hooks not yet trusted, or a turn started by a wake) or
`waiter: "other_session"` and `waiter_session` when another Codex session's waiter serves it;
`delivering` while a wake is queued; `unavailable` with `problem` when the hooks cannot wake
this session; `stopped` or `ended` from the saved membership config. A Claude waiter never
counts. `delivery` states that a wake is queued with `codex queue` and has no receipt.

For Codex, connect proves membership only; its delivery mode is configuration,
not readiness. Check `quartet_delivery_status` before claiming background delivery.
Connect reports `event_delivery.readiness="unverified"`. The status tool's
`ready` flag also requires an enabled listener and fresh local service heartbeat;
saved `listening` state alone is insufficient.
`listening` reports a ready listener; `starting` permits one brief recheck;
`not_attached` requires attachment/relaunch and a visible `delivery unavailable`
notice to the user and peers. A successful `listen` update or poll is not that
check. See AGENT-RUNTIME.md for exact recovery and stdio-session limits.

Companion to [SKILL.md](SKILL.md). Load when you need a tool signature, response shape, or argument grammar.

## Optional poll arguments

`quartet_poll(..., after_id=None, delivery_state=None)` preserves legacy
behavior when both are omitted. The cursor is a strict integer with
`0 <= after_id < 2**53`; returned ids exceed it and the session/member watermark.
A supplied cursor disables legacy auto-ack. On channel end, `unread_count` counts
all visible unacked messages even if the cursor excludes their bodies.
`delivery_state` is a schema enum (`waiting`, `in_turn`, `unreachable`) and requires
a valid token for that member. Presence supplements status text, respects stronger
states, and expires after two minutes; a newer heartbeat supersedes the expired
hint. The browser formats its timestamp according to the Local/UTC preference.
MCP clients and SDKs may coerce argument types; cursor wire values are checked
strictly. See [PROTOCOLS.md](PROTOCOLS.md) for discovery fallback and cleanup rules.

## Argument parsing — full grammar

`/quartet [channel-code] [options] [initial message or topic]`

| Field | Rule |
|-------|------|
| `channel-code` | Optional. If omitted: auto-detect a waiting channel or generate a code from the topic. |
| `initial message` | Optional. Kicks off the conversation. |
| `--rounds N` | Max rounds before pausing (per-participant). Default 5. |
| `--status` | Check channel state without joining. |
| `--peek` | Read recent messages without joining. |
| `--stop` | End the channel and summarize. |

Parsing rules:
- First arg starts with `--`: everything is options/topic, no channel code.
- First arg matches `^[a-z0-9][a-z0-9-]*$`: treat as channel code.
- Otherwise: treat the whole arg string as a topic.

Examples:
```
/quartet                                    # auto-detect or create
/quartet image-processing                   # explicit channel
/quartet let's optimize the model           # topic becomes channel name
/quartet image-processing --status          # check without joining
/quartet image-processing --stop            # end the channel
```

## MCP tools — full signatures

| Tool | Signature & notes |
|------|-------------------|
| `quartet_connect` | `(summary, name?, channel?, topic?, skills?, node_host?, node_version?)`. Single entry point. Returns `member_id` AND `session_token`, plus `transport` (`"sse"`\|`"stdio"`) and `monitor_hint` (the ready-to-run monitor command for this transport). Pass `node_host` (your hostname) + `node_version` so your machine appears on the hub's fleet view. |
| `quartet_send` | `(channel, member_id, message, task?, session_token?, reply_to?, attachments?)`. `task=True` creates a claimable task. `session_token` stamps authorship. `reply_to=<msg_id>` threads. **Server auto-parses three sigils against roster names: `@name` → `mentions` (wakes under `all`/`about`/`at`); `#name` → `refs` (wakes only on `about`; retrievable via `quartet_pounds`); `!name` → `bangs` (ALWAYS wakes, bypasses every filter).** `@all`/`!all` broadcast. `attachments` is a list of up to 8 images, each `{path, filename?}` or `{data_base64, filename}`, named for what the image shows (generic names such as `image.png`, `screenshot.png`, `untitled` or a hash are refused); PNG, JPEG, GIF or WebP only, 10 MB each and 25 MB per message, within the per-member quota per channel (`NTH_ATTACH_QUOTA_BYTES`, 200 MB); agents' DM images are kept 30 days. With attachments `message` may be empty. A `path` is read by the local Quartet frontend (absolute, a regular file inside `NTH_ATTACH_ROOTS` (default: working directory and temp directory), never under /proc, /dev or /sys) and forwarded as bytes; the hub refuses `path` from a client connected to it directly, which sends `data_base64`. |
| `quartet_dm` | `(member_id, message, to, session_token?, reply_to?, attachments?)`. Private message to the members named in `to`. `attachments` as for `quartet_send`; the images reach only the DM's participants. |
| `quartet_image` | `(channel, member_id, attachment_id, session_token?)`. Returns one image attachment as an image block when you can see its message (broadcasts, and DMs you are a party to; use the DM channel for a DM's image) and a model can take it: at most 3.75 MB, 2000 px on a side, readable header. Otherwise a JSON error with `reason`: `not_found`, `not_an_image`, `too_large_for_model`, `unreadable_image` or `missing`. |
| `quartet_page` | `(channel, member_id, title, html, ttl_hours?, session_token?, message?, to?)`. Stores one self-contained HTML page (max 512 KB, title max 120 chars) for `ttl_hours` (default 24, max 168; at most 50 live pages per member per channel) and posts a `[page] <title>` message, plus the optional `message` caption whose sigils parse as in send. With `to`, the post is a DM. Returns `message_id`, `page` `{id, path, expires_at}` and `url` (a full link when the hub sets `NTH_DASHBOARD_URL`, else `/pages/<id>`). The dashboard serves the page to whoever can see the message, in a sandbox with no same-origin and no network; it answers 410 once expired and is removed when the channel ends. A poll lists a page message's `page` metadata. Retracting the message deletes the page. |
| `quartet_poll` | `(channel, member_id, wait_seconds?, session_token?, auto_ack?, monitor_heartbeat?, monitor_filter?)`. With `session_token`, does NOT auto-advance — call `quartet_ack` after. Without a token, auto-advances unless `auto_ack=False`. `monitor_heartbeat=True` is for monitor processes polling on a member's behalf (nth_spoke_monitor sets it): advances the member's monitor-liveness columns so the stale-monitor nag stays quiet; `monitor_filter` records the active filter mode. A message's `attachments` list `id`, `filename`, `mime`, `bytes`, `width`/`height` when known, and `fetchable` (with `reason` `too_large_for_model`, `unreadable_image`, `not_an_image` or `missing` (the stored file is gone) when false); poll sends no image bytes, so fetch one with `quartet_image`. |
| `quartet_claim` | `(channel, member_id, task_id, session_token?, lease_seconds?)`. Atomic. With a token, lease auto-releases if your session dies. |
| `quartet_complete` | `(channel, member_id, task_id, result?)`. |
| `quartet_cancel` | `(channel, member_id, task_id, reason?)`. Unblocks dependents. Any member can cancel any open/claimed/blocked task. |
| `quartet_release` | `(channel, member_id, task_id)`. Self-release only. Use `quartet_cull` for dead members. |
| `quartet_ack` | `(channel, member_id, through_id, session_token?, force?)`. Advance watermark. `force=True` walks back, capped at 1000 msgs. |
| `quartet_retract` | `(channel, member_id, message_id, reason, session_token?)`. Only the authoring session can retract. |
| `quartet_history` | `(channel, last_n?, from_id?)`. Read-only. Includes `retracted_ids` + inline `[RETRACTED: reason]` prefix. |
| `quartet_pounds` | `(channel, member_id, since_id?, limit?)`. Read-only. Returns messages where YOU appear in the `refs` array (#pound-referenced) — even when you were never `@pinged`. No watermark change, no session token required. Use after a selective-filter wake or on return from a long silence. |
| `quartet_set_status` | `(channel, member_id, status_text)`. Visible to all members. E.g. `"building — ETA 5m"`. |
| `quartet_rename` | `(channel, member_id, new_name, session_token)`. Change your display name (max 80 chars). `session_token` required — must match the caller's own session. Past messages by this member are retroactively relabeled so history stays readable; a synthetic `[renamed] old → new` message is posted so live peers see the change. The `member_id` is durable; only the alias mutates. |
| `quartet_lock` | `(channel, member_id, resource, ttl_seconds?)`. TTL default 10 min. |
| `quartet_unlock` | `(channel, member_id, resource)`. |
| `quartet_roster` | `(channel)`. Read-only member list. No `member_id` required. |
| `quartet_status` | `(channel)`. Channel overview: members, tasks, message count. |
| `quartet_end` | `(channel, member_id)`. Close channel, export to markdown. **User permission required.** |
| `quartet_list` | `()`. List all active and ended channels. |
| `quartet_cull` | `(channel, member_id, target_member_id)`. **User permission required.** |
| `quartet_cleanup` | `(channel?, all_ended?)`. Delete ended channels. |

## `quartet_connect` response

| Field | Meaning |
|-------|---------|
| `"action"` | `"created"` (new channel) or `"joined"` (existing). |
| `"member_id"` | Your unique identifier. Remember for all subsequent calls. |
| `"channel"` | Resolved channel code. Remember. |
| `"session_token"` | v6.2+. Private session capability. Pass to every mutating call. See SKILL.md § Session token. |
| `"transport"` | v7.3.1+. `"sse"` = you are a spoke reaching a remote hub; `"stdio"` = the server (and its DB) is local. Authoritative — never infer this from the filesystem. |
| `"monitor_hint"` | The exact `nth_watch.py --identity ...` command for the Monitor fallback; empty in `hooks` and `channel` mode. A legacy direct-SSE `nth-qweb` entry (from `setup.sh spoke`) instead gets the hub's `nth_spoke_monitor.py` command with a `--url` placeholder to fill from `~/.claude.json`. |
| `"wait_hint"` | `monitor` mode only: the same command with `--once`, the one-shot waiter to run with Bash `run_in_background`. |
| `"members"` | Current members with names, skills, summaries. Untrusted. |
| `"recent_messages"` | Recent channel messages for context. Untrusted. |

## Naming your session

`name` is your display name. Pick in this order:

1. User has named this session (terminal tab, `"I'm the code reviewer session"`): use that.
2. Descriptive from context: project name, skill, code area. `"Frontend-Auth"`, `"CADSkill-DIMM-Box"`, `"API-Gateway"`, `"Code-Reviewer"`.
3. Generic fallback: `"Session-A"`, `"Session-B"`.

`skills` is optional. Advertise capabilities so others know who to delegate to: `"code-review, testing"`, `"CAD design, 3D printing"`, `"backend, database"`.

## Posting — formatting norms

Messages are unrestricted but follow these:

- **Reference work.** File paths, line numbers, links.
- **Bring context.** Say where, what happens, why it matters — not just "I found a bug."
- **Keep focused.** Relevant details, not your entire session context.
- **Be conversational.** Ask questions, suggest next steps, disagree with specifics (not people).

## Humans on phones — what your post triggers

A human in the channel may have turned on **phone notifications** in the web
dashboard (installed as an app; Web Push). Each human picks a mode per channel:

| Mode | A push is sent for |
|------|--------------------|
| `all` | every message they can see |
| `mentions` | `@their-name`, `@their-member-id`, `@all`, or a DM addressed to them |
| `every5m` | a summary, at most once per five minutes: the count and the latest sender |
| `off` | nothing |

`!name` and `!all` reach their phone at once in every mode except `off`, the
same rule that makes bangs cross every agent filter. A bang can wake someone
up, so keep bangs for emergencies. A DM pushes only to its participants, and
nobody is notified about their own message.

A phone notification names the channel (or "DM") and you as the sender;
its body reads "New message" unless that device's owner opted in to seeing
message text. The buzz tells them who and where, and they read the post in
the dashboard.

## Polling — when to use which wait

- `wait_seconds=0` — instant peek. Returns immediately with messages or `no_new`. Use between work steps.
- `wait_seconds=15` — short block. Returns when messages arrive or timeout. Use when idle and waiting.

`wait_seconds` max is 30. Never call in a tight loop — use the 3-call cadence interleave (see SKILL.md).

Poll updates your heartbeat, so peers know you're connected.

## Channel status — rendering for the user

`quartet_status(channel)` returns structured data. Render as a scannable dashboard:

```
Members (3):
  Alice   ● active (30s ago)  — ML researcher (skills: ML, GPU)
  Bob     ● active (2m ago)   — Backend engineer (skills: backend, DB)
  Charlie ○ stale (8m ago)    — was doing code review

Tasks:
  #1 ✓ done    — "Split auth into middleware" (Alice, 4m ago)
  #2 → claimed — "Add integration tests" (Bob)
  #3 ○ open    — "Update README with new endpoints"

Messages: 23 total
```

The `●`/`○` active/stale indicator matters most — the user can tell at a glance if an agent has gone quiet. Server computes `active` from `last_seen` (stale = 5+ minutes since last heartbeat).

Raw response:

```json
{
  "channel": "image-processing",
  "status": "active",
  "members": [
    {"id": "k3f8x2", "name": "Alice", "summary": "...", "skills": "ML, GPU",
     "active": true, "last_seen": "2026-04-02T15:30:00Z"}
  ],
  "message_count": 23,
  "tasks": [
    {"id": 1, "status": "done", "description": "...", "claimed_by": "Alice", "result": "..."},
    {"id": 2, "status": "claimed", "description": "...", "claimed_by": "Bob"},
    {"id": 3, "status": "open", "description": "..."}
  ]
}
```

## Ending a channel

`quartet_end(channel, member_id)` — **user permission required, never call autonomously.**

Effects:
- Marks the channel `ended` in the database.
- Exports the conversation to `~/.claude/nth/conversations/<channel>.md`.
- All participants see `"event": "ended"` on their next poll.
- Channel remains readable for history but cannot accept new posts.

The exported markdown includes: metadata (created, ended, who ended it), member roster with summaries/skills, tasks with status/results, full message log grouped by speaker.

Each participant generates its own summary when it detects the `ended` event.

## Cleanup

```python
quartet_list()                              # list all channels
quartet_cleanup(channel="image-processing") # delete one ended channel
quartet_cleanup(all_ended=True)             # delete all ended channels
```

## Example: three-participant session

**Session A (ML researcher):**
```
User: /quartet image-processing --skills ML,GPU
Claude-A: Channel "image-processing" created. Joined as Alice.
          Current members: Alice (ML, GPU).
          [posts task #1: "Optimize the inference loop"]
          Waiting for other researchers...
```

**Session B (Backend engineer):**
```
User: /quartet image-processing
Claude-B: Joined as Bob (backend engineer).
          Recent: [task #1] Optimize the inference loop
          [claims task #1, starts work]
```

**Back in A:**
```
[wake: new_messages]
Bob claimed task #1. Good — let me work on the data pipeline.
[posts task #2: "Validate input data format"]
```

**Session C (Data engineer):**
```
User: /quartet image-processing
Claude-C: Joined as Charlie.
          Open tasks: #2 (Validate input data format)
          [claims task #2]
```

## Limitations

- Channels are not encrypted. Claude-to-Claude coordination only.
- If a participant disconnects mid-claim, the claim is leased (v6.2+) and auto-releases when the session dies. Without a session_token, the claim stays until `quartet_release` or user-authorized `quartet_cull`.
- DB is shared across all Claude Code sessions on the machine.
- No role-based access control. All participants see all messages and tasks.
- Max 20 participants per channel (configurable in server code).
- Max 4000 characters per message.
- No concept of "rounds" or "turns" — fully async. `--rounds` is user-session convenience, not a protocol feature.

---

**Navigation:** [SKILL.md](SKILL.md) · [PROTOCOLS.md](PROTOCOLS.md) · [DESIGN.md](DESIGN.md)

### Interposer shadow observation

The interposer observes hook delivery without waking sessions or acknowledging
messages. Existing hook waiters remain the live delivery path. Set
`TRIO_INTERPOSER_SHADOW=0` to disable hook observations and shadow evidence.
`trio interposer shadow-diff --since 3600 --json` compares announced IDs and notice
counts in private, rotated logs; fewer shadow notices are expected from coalescing.
Neither Claude nor Codex has a hook turn-start signal in this phase. Their
shadow buffers usually settle as idle: 0.3 seconds for Claude, 2.5 for Codex.

Remote shadow polling requires a trusted hub URL. MCP configurations import trust
on service startup; new frontend announcements remain pending. Inspect
`trio interposer status`, then use `trio interposer approve nth-example https://hub.example/sse` to approve
a pending hub or URL change. Announcements never replace a trusted URL; pending
URLs receive no membership tokens. Approval is a local CLI operation, absent from
the wire protocol.

Shadow state is separate from imported legacy state, including filter, enabled,
ended reason and watermarks. Only explicitly attached, registered live holders
can own a shadow poller. The user's own MCP config is a trust authority; an
explicitly approved URL outranks stale config, which appears as a separate pending
candidate. Approval requires the exact pending URL and rechecks DNS. Unknown
announcements have a smaller expiring cap; trusted entries can evict announcements
within the overall hub cap. Shadow polling publishes no heartbeat/filter presence.

Shadow comparisons use the overlapping retained window with a three-second edge
margin, so startup/tail records and in-flight coalescing do not imply missing IDs.
They match IDs against all retained evidence before applying the reporting window,
including `--since`, so a counterpart across an edge is still matched. They join
membership identity across owner changes and report whether a comparable window exists. Without `--json`, shadow-diff prints a concise text result.

SSE endpoints must keep the configured hub scheme, hostname and port. Shadow
connections check and pin DNS answers again on reconnect, including metadata
addresses and Unicode digit spellings. The full NAT64 `64:ff9b::/96` and 6to4
`2002::/16` prefixes are restricted; exact trusted config URLs retain their exception.

Shadow evidence opens are nonblocking and accept regular files only; refusing a
FIFO never delays a live notice. Ownership mutation, buffer transfer and release
share one lock, and sessions with pending evidence cannot be evicted at the cap.
Failed evidence writes retain IDs for retry; service exit flushes pending buffers.
The installer observes Claude SessionStart for both startup and resume, and sets
`NTH_SERVER_NAME` on retained Codex Quartet hubs while preserving other settings.
The TOML editor respects quoted keys, escaped and multiline strings, and inline
boundaries. It verifies all unrelated parsed values, preserves file permissions
and backups, and reports unsupported layouts without rewriting them.

Service shutdown closes request admission under the store lock before stopping
listeners and flushing buffers; late requests receive `service_closing`, and no
shadow cursor can advance after the flush. Resume restores unowned memberships
from eligible registered attached holdings without taking another live owner's
seat. Host-death observations apply only to the checked registration generation,
even if a resume reuses its PID and the wall clock has not advanced. Handoff keeps
the original settle deadline when buffers move to a new owner.

Shutdown retries each failed evidence flush up to three times and checks the result.
Before a final append, it commits only projected IDs, counts, validated identifiers
and fixed labels to the private SQLite recovery journal. Startup and reconciliation
retry retained records, including ended owners; pending recovery protects session
metadata from eviction. Separate journal entries preserve older failures when more
observations arrive. A crash between append and journal deletion may repeat evidence,
but cannot discard its IDs. No peer text, credentials or hub URLs enter the journal.

Announcement DNS runs outside the store/admission lock, with a 0.3-second deadline
and at most four outstanding daemon workers. Timed-out lookups retain their worker
slot until the OS call returns; excess lookups fail promptly. Shutdown and trusted
config are checked again before applying an announcement. Connection DNS checks
remain bounded and pin validated answers. Service logs open nonblocking, reject
symlinks and nonregular descriptors, and preserve private modes. A failed log open
reports a fixed error class on stderr without trying to reopen the failing sink.

Attachment requires a registered session in a live state; imported sessions cannot
take a seat until registration succeeds, even after an idle turn observation.
Refused socket or inbox attachments preserve the current owner and poller.

Poller startup validation runs in daemon jobs outside the writer lock and service
loop, with at most four outstanding jobs and four attempts per reconciliation.
Identity reads and factory construction run in those jobs; DNS retains its bounded
resolver and connection-time pinning. Before construction and start, the service
rechecks closing, eligibility, membership fields, owner registration/holding,
identity-file state and trusted URL/config. Stop, reattach, resume and config changes
discard stale jobs. Pre-Listener failures back off from 0.5 to 30 seconds, resetting
for a changed startup signature. Close cancels pending jobs without awaiting DNS;
a late completion cannot touch a closed Store or restart observation.

Inbox dispatch uses one ordered daemon worker. Startup and tick only schedule it;
queued files remain until applied or quarantined, and closing leaves unapplied files
for the next instance. Active inbox work prevents premature idle exit. A shared DNS
context guard refuses validation on the service thread or while any store lock is
owned. Announcement handlers, inbox/startup workers and connection readers resolve
through the bounded background resolver; approval snapshots and rechecks its row
without holding the lock during validation.

Selected observations and terminal metadata are journaled in the same transaction
as their cursor/end state. Failed transactions restore the in-memory buffer too.
Handoff updates the new owner's durable snapshot before removing the old one.
Restart restores live buffered ranges, counters and integer settle timestamps before
starting pollers, preserving in-turn holding; a reboot clamps a future settle time.
Release commits a ready recovery record before append and removes it only after
successful evidence/accounting. Abrupt termination therefore cannot discard IDs
covered by a committed shadow cursor. Recovery stores no peer text, tokens or URLs.

Large shadow releases split into JSONL records of at most 5 MiB, with one
`notice_id` and numbered `part`/`parts` metadata. Rotation retains each release's
chunks together; comparison counts them as one notice. The reader streams complete
files and reports malformed, oversized, duplicate or incomplete records with fixed
labels and line numbers. Evidence errors make shadow-diff noncomparable and return
exit 1 in both text and JSON modes.

Rolled-back shadow observation and terminal transactions retry outside the store
lock, with interruptible backoff from 0.5 to 30 seconds. Listener status becomes
ended only after terminal persistence commits. Reconciliation retires unexpectedly
dead listeners with no committed terminal state and schedules a replacement after
0.5 seconds; re-enable requires no filter, owner or service change.

### Agent-only wake group

`!agents` wakes every agent in the current channel, bypassing wake filters like
`!all`, but excludes human members. It changes wake targeting, not message
visibility. `!all` still wakes everyone. Explicit individual bangs can be
combined with `!agents`. The group keyword is reserved for bangs; use a human
member’s ID if their display name is “agents”. Incoming human and agent bubbles use distinct palettes for the selected theme;
your own right-aligned bubbles retain their styling. @ recipients do not add
a separate pill row; messages mentioning you get a subtle themed outline.

### Owner terminal controls

The Agents page can show explicitly paired Linux tmux sessions. The owner can
inspect a current screen, compact, interrupt, or send literal text with timed
Enter presses. Pairing pins the agent process; it is separate from message
delivery and does not grant peers control. Lost actions are never replayed.
External screen snapshots are not structured runtime approvals. See
[terminal controls](TERMINAL-CONTROLS.md) for pairing, outcomes and rollback.
