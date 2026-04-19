# Trio v7 — Notify-Push Design Proposal

> **Status:** Proposal. Not yet implemented.
> **Author:** Drafted 2026-04-19 in conversation with Gabe (Roam Gen2 project).
> **Scope:** Replace polling in the trio sentinel with filesystem-event
> push; add targeted notification; surface delivery receipts. Everything
> else (protocols, tool surface, DB schema semantics) stays the same.

---

## 1. Motivation

The current trio (v6.x) design has the right shape — SQLite-backed MCP
channel, per-session background sentinels watching for relevant events,
event-driven bubble-up to the parent Claude via task-notifications — but it
has three problems at realistic multi-Claude volume:

### 1.1 Active latency is tied to a magic-substring mode switch

`nth_sentinel.py` picks its poll interval by scanning `status_text` for
substrings: `idle`, `standing by`, `tier 3`, `agent-monitor`. If present,
the interval flips from 3s (active) to 30s (idle/sleep), with a 60s
sleep-confirmation hysteresis. This is surprising — two peers can see 10×
different end-to-end latency for the same message because one's status
happens to contain a trigger word. The dependency chain is also fragile:
any status-text edit is a de-facto interval change.

### 1.2 Every message wakes every peer

Currently when any member posts to a channel, every other member's
messenger sentinel detects the row on its next poll and fires up to its
parent Claude. Each fire produces a `<task-notification>` block in the
parent's context (~250-300 tokens), persistent until compaction.

In a 4-Claude channel with typical working volume (~20-30 messages/hour
aimed at each peer — so ~60-90/hour total received by each member), a
single peer accumulates 500-700 received messages over an 8-hour day.
At the current notification cost:

- **Broadcast model (today):** ~150K-215K tokens/day/peer of notification
  envelope alone. Half of a 200K context window lost to task-notifications
  for messages that often weren't even for this peer.
- **Idle keepalive (today):** ~600 tokens/hour/session from two sentinels
  restarting every ~59 min — ~14K tokens/day if idle.
- **Combined:** context fills aggressively in any sustained multi-Claude
  work session.

### 1.3 Senders are blind to delivery

There's no surface today that tells a sender "my message was actually read
by peer X at time Y." `last_read` is tracked per-member and per-session,
but it's not exposed. Senders wait on observable side-effects (a reply, a
tool call) to confirm the receiver picked up the message. This is fine for
small channels but doesn't scale; and for directed-work patterns, knowing
"Cloud read my dispatch but App hasn't yet" is genuinely useful.

---

## 2. Goals

1. **Replace polling with filesystem-event push.** Target <50ms active
   latency from `trio_send` commit to peer's sentinel wake.
2. **Targeted delivery.** Messages not addressed to a given peer don't
   wake that peer's sentinel at all — so they don't pollute that peer's
   parent Claude's context.
3. **Delivery receipts.** Expose `last_read` watermarks to senders via the
   existing `trio_poll` response. Sender learns "who has read what I said"
   on their next poll.
4. **Mode simplification.** Eliminate the active/idle/sleep polling mode
   switch. One push-based loop.
5. **Sentinel consolidation.** Merge the two sentinels (messenger +
   watchdog) into one.
6. **Backward compatibility.** Old clients that don't write to notify
   files must still function. The `to=` parameter must be optional
   (default = broadcast, matching current behavior).
7. **Filesystem-contained.** No broker daemon, no socket server, no
   long-running process beyond what exists today.

### 2.1 Non-goals (explicit)

- **No batching.** Keep notification-per-message 1-to-1. Batching was
  considered and rejected as premature optimization — adds scheduling
  complexity, edge cases around urgent/interrupt, and is easily retrofit
  later if measured context cost still hurts.
- **No priority tiers / silent notifications.** Every event bubbles up
  1-to-1 as today.
- **No cross-machine trio.** One machine, shared filesystem.
- **No DB schema change.** Piggyback on existing `last_read`; don't
  introduce a `message_reads` table.
- **No MCP protocol redesign.** Additive changes only.

---

## 3. Design Overview

### 3.1 Control flow — before

```
Claude A                MCP-A      SQLite        MCP-B      Claude B's sentinel    Claude B
  │ trio_send ──────────>│         │             │          │ (polling SQLite       │
  │                      │─INSERT─>│             │          │  every 3s)            │
  │<── ok ───────────────│         │             │          │                       │
  │                      │         │             │          │   poll → row found    │
  │                      │         │             │          │   exit with event ───>│ <task-notification>
  │                      │         │             │          │                       │ trio_poll
  │                      │         │             │<─SELECT──────────────────────────│
  │                      │         │             │── rows ─>│                       │
```

End-to-end latency: **0-3000ms** (poll phase) + **~1-2s** (subagent bubble-up)
+ **parent-Claude scheduling delay**.

### 3.2 Control flow — after

```
Claude A                MCP-A      SQLite    Notify FS    MCP-B      Claude B's sentinel    Claude B
  │ trio_send ──────────>│         │         │            │          │ (blocked on kqueue   │
  │                      │─INSERT─>│         │            │          │  over notify file)   │
  │                      │─utime──────────── >│           │          │                       │
  │<── ok ───────────────│         │         │            │          │                       │
  │                      │         │         │── event ──────────────│ wake                  │
  │                      │         │         │            │          │   query DB            │
  │                      │         │         │            │<─SELECT──│                       │
  │                      │         │         │            │── rows ──│                       │
  │                      │         │         │            │          │   exit with event ───>│ <task-notification>
  │                      │         │         │            │          │                       │ trio_poll
```

End-to-end latency: **~10-50ms** (kqueue wake) + **~1-2s** bubble-up +
parent-Claude scheduling delay. Filesystem push replaces SQLite polling.

### 3.3 Component summary

| Component | Changes |
|---|---|
| `nth_server.py` (MCP) | Add `notify()` helper. Call on every state-mutating tool. New optional `to=` on `trio_send`. `@Name` parser. Extended `trio_poll` response with `delivery` array. |
| `nth_sentinel.py` | Replace `time.sleep(interval)` with kqueue/inotify wait. Merge messenger+watchdog into one. Remove active/idle mode switching. Keep 30s backstop poll. |
| New `notify_watcher.py` | Stdlib-only. kqueue on macOS, inotify on Linux, `ReadDirectoryChangesW` on Windows, stat-poll fallback. ~80 LOC wrapper with a simple `wait_for_change(paths, timeout)` API. |
| `messenger-foreground.py`, `sentinel-foreground.py` | Consolidate into one `sentinel-foreground.py`. Drop the split. |
| `trio/*.md` (docs) | Update PROTOCOLS, REFERENCE, DESIGN to reflect notify semantics, delivery receipts, `to=` parameter. |

---

## 4. Filesystem Layout

Notify directory: `~/.claude/nth/notify/`

```
~/.claude/nth/
  nth.db                 (existing)
  conversations/         (existing — trio_end exports)
  notify/                (NEW — created lazily on first send)
    <channel>/
      _broadcast         ← touched for unaddressed messages
      <member_id_1>      ← touched when specifically addressed to this member
      <member_id_2>
      ...
```

**Semantics:**

- Files are zero-byte. Only their `mtime` matters.
- A channel's notify directory is created on first `trio_send` to that
  channel. Removed on `trio_end` as cleanup.
- Each member's per-member file is created the first time that member is
  addressed specifically. Removed when they leave the channel or the
  channel ends.
- Each member's sentinel watches **two** fds: its own per-member file
  and the channel's `_broadcast` file.

**Why files, not a socket:**

- No broker lifecycle. Every MCP server process can `utime()` independently.
- No startup race. Files exist after first write; sentinels create-if-missing
  on startup.
- Filesystem events are push-quality latency (~10ms on macOS / Linux)
  without any long-running service.
- Multi-reader fan-out is free — every watcher wakes on the same event
  without any message-queue semantics to get wrong.
- Works with existing Claude Code process model (stdio MCP servers).

---

## 5. Protocol Changes

### 5.1 `trio_send` — add `to=` parameter

```
trio_send(
    channel: str,
    member_id: str,
    message: str,
    session_token: str = "",
    task: bool = False,
    pin: bool = False,
    reply_to: int | None = None,
    blocked_by: str = "",
    to: list[str] | None = None,    # NEW
) -> {message_id, ok, ...}
```

**`to=` semantics:**

- `None` (default, with no `reply_to` / no `@mentions`): broadcast. Server
  touches `notify/<channel>/_broadcast`. Backward-compatible; existing
  callers unchanged.
- `["Cloud"]`, `["Cloud", "App"]`: directed. Server touches
  `notify/<channel>/<member_id>` for each named member, resolving names
  case-insensitively via `members.name`. Does NOT touch `_broadcast`.
- Unknown names: ignored (not an error — a typo shouldn't fail the send).
  Server returns `ok=True` with a `warnings` field listing unresolved names.
- Empty list `[]`: treat as `None` (broadcast). Defensive default.

**Effective target set** = `to` ∪ `@mentions` in content ∪ (author of
`reply_to` message, if set) — unless `broadcast=True`, which forces
`_broadcast` and ignores all three.

If the effective target set is empty, the message broadcasts (touches
`_broadcast`). If the set is non-empty, only the per-member files in the
set are touched, and `_broadcast` is not.

### 5.2 `@mention` parsing

Server-side: before touching notify files, scan message content for
`@<name>` tokens where `<name>` matches an existing member in the channel
(case-insensitive, word boundary). Each found mention is added to the
effective `to` set, in addition to any explicit `to=` argument.

```
trio_send(channel, member_id, "@Cloud can you handle X?")
# Effective to = ["Cloud"] (derived from @mention)
# Touches notify/<channel>/<cloud_member_id>, not _broadcast.
```

Mentions are a UX affordance; `to=` is the machine-readable form. Either
works; they compose.

Opt-out: if the caller wants to deliberately broadcast a message containing
an `@` without triggering targeted notify, they can pass `to=[]`... but
we said `[]` means broadcast. We need a real opt-out. Options:

- Add `broadcast=True` to force broadcast regardless of mentions. Cleaner.
- Or require `to=` to be explicit to override mention-parsing.

**Decision:** add `broadcast: bool = False` to `trio_send`. When true,
always touches `_broadcast`, ignores `to` and any parsed mentions. Useful
for status posts that happen to contain a name ("reviewing @Cloud's work").

### 5.3 `trio_poll` — add `delivery` field to response

Today's response (approx):

```json
{
  "event": "new_messages" | "no_new" | "ended",
  "unread_count": 4,
  "messages": [...]
}
```

New response:

```json
{
  "event": "new_messages" | "no_new" | "ended",
  "unread_count": 4,
  "messages": [...],
  "delivery": [
    {
      "message_id": 5,
      "read_by": [
        {"member": "Cloud", "at": "2026-04-19T14:23:05Z"},
        {"member": "App", "at": "2026-04-19T14:23:08Z"}
      ],
      "pending": ["Firmware"]
    }
  ]
}
```

**Rules:**

- `delivery` is always present (may be `[]`).
- Only messages authored by the polling member appear.
- Only receipts that are *new since this member's previous poll* are
  reported. Once reported, they aren't re-reported.
- `read_by[].at` is derived from `sessions.last_read_at` or
  `members.last_read_at` as available; best-effort timestamp.
- `pending[]` lists members whose `last_read < message_id` at the time
  of this poll.

**How "new since last poll" is tracked:**

Add a lightweight per-poller watermark: the highest `read_by.at` already
reported to this member. On poll, server reports receipts with
`read_by.at > last_reported_watermark`. Stored per member (not per
session), since it's about what the human-facing Claude has already seen.

### 5.4 No other tool signatures change

`trio_poll`, `trio_ack`, `trio_connect`, `trio_retract`, `trio_claim`,
`trio_complete`, `trio_cancel`, `trio_release`, `trio_set_status`,
`trio_end`, `trio_lock`, `trio_unlock`, `trio_roster`, `trio_status`,
`trio_list`, `trio_history`, `trio_cleanup`, `trio_cull` — all unchanged.

---

## 6. Server-Side Changes (`nth_server.py`)

### 6.1 `notify()` helper

```
def notify(channel: str, members: list[str] | None = None):
    """Touch notify files to wake waiting sentinels.

    members=None → touch _broadcast (everyone wakes).
    members=[id1, id2] → touch only those members' per-member files.
    """
```

Called at the end of every state-mutating MCP call that should wake
another member's sentinel. Specifically:

| Tool | notify target |
|---|---|
| `trio_send` | `to=None` → `_broadcast`; otherwise per-member files from resolved `to` + parsed @mentions |
| `trio_ack` | author's per-member file (one per distinct message author whose watermark was crossed) |
| `trio_retract` | `_broadcast` (retractions affect everyone's view) |
| `trio_claim` | task creator's per-member file |
| `trio_complete` | task creator's + any member who asked to be watched |
| `trio_cancel` | task creator's + claimant's |
| `trio_release` | task creator's |
| `trio_end` | `_broadcast` |
| `trio_set_status` | no notify (status is polled on-demand in `trio_roster`/`trio_status`) |

### 6.2 @mention parsing

```
def parse_mentions(content: str, channel: str) -> list[str]:
    """Return distinct member names mentioned via @Name tokens."""
    # Match @Name with word boundary, case-insensitive
    # Resolve against members.name for this channel
    # Return member_ids
```

Called inside `trio_send` before building the `to` set.

### 6.3 Delivery-receipt tracking

Table additions (non-breaking):

```sql
-- Already exists: members.last_read
-- Optionally already exists: sessions.last_read

-- NEW (if not present): per-member watermark of "last delivery-receipt
-- batch reported back to this member in trio_poll"
ALTER TABLE members ADD COLUMN last_delivery_reported_at TEXT;
```

In `trio_poll`, after handling new messages:

```
# Find messages authored by this member
authored = db.execute(
    "SELECT id FROM messages WHERE channel = ? AND member_id = ?",
    (channel, member_id),
).fetchall()

# For each authored message, find peers whose last_read advanced past it
# since last_delivery_reported_at.
delivery = []
for msg in authored:
    reads = db.execute(
        """SELECT member_id, name, last_read_at FROM members
           WHERE channel = ? AND last_read >= ? AND last_read_at > ?
           AND member_id != ?""",
        (channel, msg.id, last_delivery_reported_at or "1970-01-01", member_id),
    ).fetchall()
    pending = db.execute(
        """SELECT name FROM members
           WHERE channel = ? AND last_read < ? AND member_id != ?""",
        (channel, msg.id, member_id),
    ).fetchall()
    if reads:
        delivery.append({
            "message_id": msg.id,
            "read_by": [{"member": r.name, "at": r.last_read_at} for r in reads],
            "pending": [p.name for p in pending],
        })

# Update watermark
db.execute(
    "UPDATE members SET last_delivery_reported_at = ? WHERE channel = ? AND id = ?",
    (now_iso(), channel, member_id),
)
```

Returns `delivery` in the poll response.

Detail: `last_read_at` needs to be written by `trio_ack` (add column if
missing). Cheap add.

---

## 7. Sentinel Changes (`nth_sentinel.py`)

### 7.1 Merged single-sentinel model

Today: two sentinel subagents per session (messenger + watchdog) with
different `watch_events` filters. They share an expensive side-behavior:
the heartbeat columns `messenger_heartbeat` and `watchdog_heartbeat` used
for peer-liveness detection.

Merge rationale:

- With filesystem push, there's no reason for messenger to run a
  different cadence than watchdog.
- Peer-liveness between messenger and watchdog was a self-check — "is my
  own other sentinel still running?" With one sentinel, there's no peer
  to check. Liveness of the session as a whole is already derivable
  from `members.last_seen` (which is touched on every sentinel cycle and
  every MCP call).
- Halves idle keepalive token cost (one subagent instead of two).
- One subagent, one task-notification per event instead of potentially
  two cross-firing.

Post-merge sentinel watches all events of interest:
`new_messages | channel_ended | cadence | flag_inconsistency | read_receipts`.

### 7.2 Wait loop — replace `time.sleep` with filesystem wait

```
from notify_watcher import wait_for_change

while time.time() < deadline:
    notify_paths = [
        f"~/.claude/nth/notify/{channel}/_broadcast",
        f"~/.claude/nth/notify/{channel}/{member_id}",
    ]

    # Wait up to backstop_interval seconds for a notify event.
    # Returns immediately (True) if a file is touched; returns after
    # timeout (False) if nothing happened — this is the safety backstop.
    # Does NOT bubble up to parent on its own — just gives the sentinel a
    # chance to run its cadence check and catch any missed notify events.
    changed = wait_for_change(notify_paths, timeout=BACKSTOP_INTERVAL)

    # Whether or not anything changed, run the same relevance check.
    # On a false-negative (missed notify), the backstop catches it.
    try:
        # --- same DB query logic as today ---
        # new peer messages
        # + new delivery receipts for my authored messages
        # + channel state
        # + cadence gap (if it's been too long since my last send)
        pass
    except sqlite3.OperationalError:
        pass

    # Fire up to parent if anything relevant found.
    # Otherwise loop.

# Deadline reached.
return {"event": "cap"}
```

`BACKSTOP_INTERVAL = 30` seconds. Two purposes:

- Safety net if `notify()` is ever skipped by a buggy MCP server call.
- Allows cadence timer to tick — the watchdog role wants to check "have I
  been silent too long?" periodically (cadence thresholds are 180s /
  600s, so 30s ticks give clean margin and fast detection).

The backstop does NOT bubble up to parent Claude on its own — it only
wakes the sentinel's internal check loop. If nothing relevant is found,
the sentinel goes back to waiting. Zero context pollution from backstop
ticks.

### 7.3 Remove mode switching

Everything that depended on `status_text` containing `idle`/`standing by`
etc. goes away:

- `sleep_confirmed` flag: removed
- `sleeping_flag` branch: removed
- `active_interval` vs `idle_interval`: removed (one behavior)
- `flag_inconsistency` event: keep the detection (it's a correctness
  check), but wire it to "did you send >1 message after marking yourself
  idle" rather than "is your poll interval wrong"

Mode selection becomes implicit: you're blocked on kqueue when idle,
processing an event when active. No enum.

### 7.4 Relevance check — extended

Current check: "are there messages from other members with id >
local_hwm?"

New check combines three things:

1. **Peer messages.** Same as today.
2. **Delivery receipts for my authored messages.** Query: since my last
   poll, did any peer's `last_read` advance past any of my `message.id`s?
   If yes, emit `read_receipts` event.
3. **Channel state + cadence** (from watchdog merger).

If any of the three fires, exit with the event. Otherwise, back to kqueue
wait.

---

## 8. New Module: `notify_watcher.py`

~80 LOC cross-platform, stdlib-only wrapper. Public API is a single
function:

```python
def wait_for_change(paths: list[str], timeout: float) -> bool:
    """Block until any of `paths` has its mtime change, or timeout.

    Returns True if a change was observed, False if timeout elapsed.
    Creates missing paths as empty files (watchers need an fd/handle).
    """
```

Backends are selected at import time via `sys.platform`:

### macOS — `select.kqueue` (stdlib)

```python
import select

def wait_for_change(paths, timeout):
    kq = select.kqueue()
    fds = []
    try:
        for p in paths:
            if not os.path.exists(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "a").close()
            fds.append(os.open(p, os.O_RDONLY))

        events = [
            select.kevent(
                fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_ATTRIB,
            ) for fd in fds
        ]
        return len(kq.control(events, 1, timeout)) > 0
    finally:
        for fd in fds:
            os.close(fd)
        kq.close()
```

### Linux — `inotify` via `ctypes` (stdlib)

```python
import ctypes
import ctypes.util

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004

def wait_for_change(paths, timeout):
    fd = libc.inotify_init1(0)
    try:
        for p in paths:
            if not os.path.exists(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "a").close()
            libc.inotify_add_watch(fd, p.encode(), IN_MODIFY | IN_ATTRIB)

        # select() on the inotify fd with timeout
        import select
        r, _, _ = select.select([fd], [], [], timeout)
        return bool(r)
    finally:
        os.close(fd)
```

### Windows — `ReadDirectoryChangesW` via `ctypes` (stdlib)

Watches the parent directory rather than individual files (Windows API
is directory-oriented). On change, check which file in the directory
was modified and filter accordingly.

```python
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

def wait_for_change(paths, timeout):
    # Group paths by parent directory to minimize handles
    dirs = list(set(os.path.dirname(p) for p in paths))
    handles = []
    try:
        for d in dirs:
            os.makedirs(d, exist_ok=True)
            h = kernel32.CreateFileW(
                d,
                0x0001,                       # FILE_LIST_DIRECTORY
                0x0007,                       # FILE_SHARE_READ|WRITE|DELETE
                None,
                3,                            # OPEN_EXISTING
                0x02000000 | 0x40000000,      # FILE_FLAG_BACKUP_SEMANTICS|OVERLAPPED
                None,
            )
            handles.append(h)

        # WaitForMultipleObjects with timeout_ms
        result = kernel32.WaitForMultipleObjects(
            len(handles),
            (ctypes.c_void_p * len(handles))(*handles),
            False,                            # wait for any, not all
            int(timeout * 1000),
        )
        return result != 0x102  # WAIT_TIMEOUT
    finally:
        for h in handles:
            kernel32.CloseHandle(h)
```

### Stat-poll fallback (unknown platforms, or if platform backend errors)

```python
def wait_for_change(paths, timeout):
    mtimes = {p: _mtime(p) for p in paths}
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in paths:
            current = _mtime(p)
            if current != mtimes[p]:
                return True
        time.sleep(0.1)
    return False

def _mtime(p):
    try:
        return os.stat(p).st_mtime_ns
    except FileNotFoundError:
        return 0
```

**Design notes:**

- Stdlib only — no third-party deps (`watchdog`, `inotify_simple`, etc.)
  would work but add install burden.
- Each platform backend is ~15-25 LOC once the imports are factored.
- Stat-poll fallback catches unknown platforms and platform-init errors
  (e.g., inotify handle exhaustion). Latency ~50ms average vs ~10ms
  native — still well inside our budget.
- All backends share the same public API (`wait_for_change(paths,
  timeout) -> bool`), so the sentinel code doesn't branch on platform.

---

## 9. Backward Compatibility

### 9.1 Old sentinels (v6) against new server

Old sentinels still poll SQLite. They'll see new messages just as they
do today. They don't know about notify files. They continue working.

### 9.2 Old servers against new sentinels

New sentinels call `wait_for_change` against a notify file. If the file
never exists (because the server is v6 and doesn't touch it), the
sentinel falls through to the 30s backstop poll. Works; just slower.

### 9.3 Old clients against `trio_poll` response with `delivery`

Additive field. Old clients ignore it. Fine.

### 9.4 `to=` on `trio_send`

Optional parameter, defaulted to `None`. Old callers don't pass it; server
broadcasts. Same as today.

### 9.5 MCP schema changes

`trio_send` gains two new optional parameters (`to`, `broadcast`).
`trio_poll` gains one optional response field (`delivery`). Both are
pure extensions — clients built against either schema version interop.

---

## 10. Performance Budget

### 10.1 Notification count (per peer per day)

Assumptions:
- 4-Claude channel
- Each member sends ~60-90 directed messages/hour (20-30 per-peer)
- 8-hour working window
- 70% targeted, 30% broadcast (estimate; adjusts with actual usage)

| Model | Wakes/day/peer | Notification tokens/day/peer |
|---|---|---|
| v6 (broadcast-all, poll) | ~500-720 | ~150K-215K |
| v7 (targeted push) | ~250-350 (broadcasts wake all + directed-to-me) | ~75K-105K |

Savings come from: non-recipients of directed messages don't wake at all.
Broadcasts still wake everyone — that's the intended semantics.

### 10.2 Keepalive cost (idle sessions)

| Model | Tokens/hour idle | Tokens/day idle |
|---|---|---|
| v6 (two sentinels, polling) | ~600 | ~14,400 |
| v7 (one sentinel, kqueue-blocked) | ~300 | ~7,200 |

Savings come from: single sentinel halves the subagent restart cycle.

### 10.3 Active-latency

| Model | Median latency (send → peer sentinel fires) |
|---|---|
| v6 active-mode (3s poll) | ~1500ms |
| v6 idle-mode (30s poll) | ~15000ms |
| v7 (kqueue push) | ~10-50ms |

### 10.4 Server-side cost

Per `trio_send`:
- 1 `INSERT INTO messages` (existing)
- 1 `os.utime()` on notify file (new, ~50µs)
- Optional: 1-N `os.utime()` for targeted per-member files (each ~50µs)

Negligible. Server TPS is unaffected.

### 10.5 Sentinel DB query cost

Each wake (notify or backstop) runs the same query bundle as today
(~1-5ms on a small DB). In the push case, wakes only happen on real
activity; in the backstop case, every 30s like today's idle mode.

---

## 11. Implementation Plan

Phased so each step is independently testable and shippable.

### Phase 1 — Notify infrastructure (foundation)

- Write `notify_watcher.py` with macOS + Linux + stat-poll fallback.
- Add `notify(channel, members=None)` helper to `nth_server.py`.
- Wire `notify()` into `trio_send`, `trio_retract`, `trio_claim`,
  `trio_complete`, `trio_cancel`, `trio_release`, `trio_end`, `trio_ack`.
- Keep sentinel code unchanged — it still polls. Notify files are written
  but nobody reads them yet.
- Smoke test: verify notify files are created and `mtime` updates.

**Merge criterion:** notify-file writes are exercised by all
state-mutating MCP calls. No behavioral change yet.

### Phase 2 — Sentinel switch to push (opt-in)

- Add `sentinel_push.py` that uses `notify_watcher` (initially alongside
  existing `nth_sentinel.py`).
- Test on a dedicated channel end-to-end.
- Measure: active-latency before/after.
- Keep old sentinels as the default; new sentinel is opt-in via env var
  or flag.

**Merge criterion:** end-to-end latency measurement shows <100ms median
on test channel.

### Phase 3 — Sentinel merge (messenger + watchdog → one)

- Replace `messenger-foreground.py` and `sentinel-foreground.py` with
  a single `sentinel-foreground.py` using the merged event set.
- Remove `messenger_heartbeat` / `watchdog_heartbeat` columns (or keep
  as deprecated).
- Update the SKILL.md sentinel-launch block to spawn one subagent.
- Remove active/idle mode switching.

**Merge criterion:** idle keepalive token measurement shows ~50%
reduction.

### Phase 4 — Targeting (`to=` and @mentions)

- Add `to: list[str] | None = None` and `broadcast: bool = False`
  parameters to `trio_send`.
- Add `parse_mentions` with case-insensitive word-boundary matching.
- Update notify() call in `trio_send` to use resolved `to` set.
- Documentation update in REFERENCE.md.

**Merge criterion:** integration test where targeted send wakes only
the addressed member's sentinel.

### Phase 5 — Delivery receipts

- Add `last_read_at` column to `members` (if not present).
- Add `last_delivery_reported_at` column to `members`.
- Populate `last_read_at` on `trio_ack`.
- Build delivery query in `trio_poll`.
- Add `delivery` to response.
- Update client-facing docs and tool REFERENCE.

**Merge criterion:** integration test — sender posts, receiver acks,
sender's next poll reports the receipt.

### Phase 6 — Max-runtime tweak

- Raise `MAX_RUNTIME_S` from 3540 to 3580 (60s safety margin → 20s).
  Marginal; can be bundled with any phase.

### Phase 7 — Documentation + cleanup

- Update `SKILL.md`, `REFERENCE.md`, `PROTOCOLS.md`, `DESIGN.md` to
  reflect new architecture.
- Delete `nth_wait.py` (already obsoleted). Delete old messenger/watchdog
  split.
- Bump version string `v6.2` → `v7`.

---

## 12. Testing

### 12.1 Unit tests

- `notify_watcher.wait_for_change`: touched file returns True within ~100ms,
  no touch returns False after timeout.
- `parse_mentions`: @Name resolution, case insensitive, word boundary,
  unknown names ignored.
- `notify()`: correct files touched for each `to` combination.

### 12.2 Integration tests

- Two-member channel, directed send: non-recipient sentinel doesn't wake.
- Two-member channel, broadcast send: both sentinels wake.
- Delivery receipts: send + ack + poll round-trip.
- Backward compat: old sentinel against new server still sees messages.
- Backstop recovery: kill the notify file between events, verify 30s
  timeout still produces the event.

### 12.3 Benchmarks

- Latency: time from `trio_send` return to sentinel's `new_messages`
  emission on a peer. Target median <50ms, p99 <200ms.
- Token cost: run a scripted conversation simulating 600 messages over
  an 8-hour window; measure task-notification bytes in the receiver's
  conversation log before and after v7.
- Idle cost: single-session, no traffic, 24h run. Measure total
  notification bytes.

---

## 13. Rejected Alternatives

### 13.1 Socket-based broker

**What:** A long-running daemon (`nth_broker`) that listens on a UNIX
socket. MCP servers publish events to it; sentinels subscribe.

**Why rejected:**

- Adds a process lifecycle to manage: who starts it, how to restart, how
  to detect failure, how to avoid startup races.
- First-run race: broker not up when first client connects.
- Silent-failure mode: broker dies, messages vanish. Requires supervisor
  or health checks, which adds complexity.
- Doesn't obviously beat notify files on any axis that matters for a
  single-machine developer tool.
- Kept in mind as an upgrade path if cross-machine trio ever becomes a
  goal. Out of scope for v7.

### 13.2 SQLite `update_hook` / WAL file watching

**What:** Watch the SQLite WAL file or `.db` file for `mtime` changes
directly; no separate notify file.

**Why rejected:**

- WAL is touched on every DB write, not just message insertion (acks,
  heartbeats, status updates, etc.). Sentinels would wake on every
  heartbeat. Noise.
- Per-channel filtering would be impossible (WAL is DB-wide).
- `update_hook` is a C API not exposed in stdlib Python.
- Notify files are strictly more controllable.

### 13.3 Per-event notify files (one per event type)

**What:** Separate notify files for messages, acks, task-claims, etc.

**Why rejected:**

- More filesystem paths to manage without measurable benefit. Sentinels
  do one DB query on wake regardless of notify granularity — granularity
  beyond per-channel / per-member doesn't reduce the query cost.
- More surface for bugs.

### 13.4 Notification batching window

**What:** Sentinel delays its exit by N seconds after first event to
collect additional events into a single task-notification.

**Why rejected (for v7):**

- Adds scheduling complexity (timer management, flush triggers,
  interrupt for urgent events).
- Priority tiers to avoid delaying @mentions add more complexity.
- Hardest-to-test failure modes (message ordering, stale batch flushing).
- Premature optimization — 1-for-1 may be fine at actual volume once
  targeting removes non-recipient wakes. If context pollution is still a
  problem, batching can be retrofit as a focused follow-up.

### 13.5 Priority tiers (urgent / batched / silent)

**What:** Three priority levels controlling whether sentinel fires up,
delays, or suppresses notifications.

**Why rejected:**

- Requires senders to classify; most won't.
- Interacts with batching and adds more state.
- 1-for-1 simplicity wins for v7.

### 13.6 Eliminate sentinels entirely, use `ScheduleWakeup` / `/loop`

**What:** Let Claude poll the channel on a deterministic schedule via
the runtime's wake-up mechanism. No background subagents.

**Why rejected:**

- `ScheduleWakeup` is a per-session runtime affordance, not a
  cross-session coordination primitive. It only works while the user is
  actively engaging Claude.
- Loses the "proactively tell me when something relevant happens" property
  that sentinels provide.
- Doesn't solve the parent-idle problem we identified separately.

---

## 14. Resolved Design Decisions

Settled during review on 2026-04-19:

1. **Notify directory location.** `~/.claude/nth/notify/` — keeps all
   trio state under one roof.

2. **Backstop cadence — 30s.** Never bubbles up to parent Claude on its
   own; it's purely an internal safety tick. Two purposes:
   (a) missed-notify recovery if the server ever skips a `utime()` call;
   (b) cadence-timer ticks for the merged sentinel's watchdog role
   (thresholds 180s active / 600s watchdog — 30s gives clean margin and
   fast detection). Since the backstop doesn't pollute context (no
   task-notification unless something relevant is found), the cost of
   staying at 30s is effectively nil.

3. **Cross-platform filesystem-event primary, with stat-poll fallback.**
   Three-way backend in `notify_watcher.py`:

   | Platform | Mechanism | Latency |
   |---|---|---|
   | macOS | `select.kqueue` (stdlib) | ~10ms |
   | Linux | `inotify` via `ctypes` (stdlib) | ~10ms |
   | Windows | `ReadDirectoryChangesW` via `ctypes` (stdlib) | ~10ms |
   | Fallback | stat-poll @ 100ms | ~50ms avg |

   Stdlib only — no `watchdog` package dependency. Each platform backend
   is ~15 LOC. Stat-poll fallback handles unknown platforms or when
   platform backend errors.

4. **Remove `messenger_heartbeat` / `watchdog_heartbeat` columns.**
   Deprecate in v7 (keep columns unused), drop in v8.

5. **`trio_set_status` does not notify.** Status is read on-demand by
   `trio_status` / `trio_roster` — neither is called in hot loops; they
   run when a Claude specifically wants to check who's in a channel or
   what their status is (on connect, on user question, on diagnostic
   paths). Members signal status changes via messages when it matters,
   and messages ARE notify-worthy. If a live-status push becomes needed
   (e.g., UI showing online/offline in realtime), it's an easy add later.

6. **`reply_to` auto-targets the original author.** Correcting the
   first-draft stance. Real semantics:

   - `reply_to=N` → message #N's author is added to the notify set
     implicitly. Matches chat-UX expectations; a reply inherently
     addresses its parent.
   - `to=[...]` composes: explicit `to` names are added on top of
     the reply_to author.
   - `@mentions` in content also compose.
   - `broadcast=True` overrides everything — explicit opt-out if
     you genuinely want to reply without notifying the author.

   Example: `trio_send(reply_to=5, to=["App"], message="@Firmware please review too")`
   → notifies msg#5 author + App + Firmware.

---

## 15. Migration Checklist (for implementers)

- [ ] Phase 1: notify infra + writes (no reader yet)
- [ ] Phase 2: push-based sentinel (opt-in)
- [ ] Phase 3: merge messenger + watchdog
- [ ] Phase 4: `to=` param + @mention parsing
- [ ] Phase 5: delivery receipts in `trio_poll`
- [ ] Phase 6: max-runtime tweak
- [ ] Phase 7: docs + cleanup + version bump
- [ ] Benchmark before and after: active latency, idle tokens,
      notification tokens/day at realistic volume
- [ ] Update all `SKILL.md` sentinel-launch blocks
- [ ] Verify backward compat against a v6 session in the same channel
- [ ] Tag release `v7.0`

---

## 16. Summary

v7 replaces SQLite polling with per-channel-and-per-member filesystem
notifications, merges the two sentinels into one, removes the
status-text-driven mode switch, adds optional targeted delivery via
`to=` and `@mentions`, and surfaces read receipts to senders via the
existing `trio_poll` response.

Active latency drops from seconds to tens of milliseconds. Idle
keepalive token cost halves. Targeted-message context pollution for
non-recipients goes to zero. No brokers, no sockets, no daemons — just
files and the kernel's event mechanisms.

Batching and priority tiers are deliberately out of scope for v7 and
can be retrofit if 1-to-1 proves insufficient at real volume.
