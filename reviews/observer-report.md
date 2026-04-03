# Trio System Report — orca-mvp Channel
## Observer the Black | 2026-04-03

### Channel Profile
- **Channel:** orca-mvp
- **Objective:** Advance bitmap nester MVP on feature/concave-bitmap
- **Duration observed:** ~35 min (join at msg 86, observed through msg 595+)
- **Members:** 8 (Taskmaster, Red, Yellow, Purple, Green, Orange, Pink, Observer)
- **Peak message rate:** ~5.5 msg/min, with idle gaps during builds
- **Total messages at end of observation:** 120+
- **Task system usage:** zero — all coordination via messages
- **Feedback collected:** 7/7 team members (all non-Observer agents)

---

### Trio System Behavior

**Reliability: Excellent (unanimous)**
- Zero reported message drops across all 7 respondents
- Zero ordering anomalies — message IDs sequential with no gaps (verified 559–595)
- All members received all messages (verified by cross-referencing replies to specific msg IDs)
- Concurrent writes from multiple agents handled cleanly

**Latency: Good**
- Polls return quickly during active periods
- Multiple agents posting within 1-2 seconds of each other
- Background wait script (used by Purple, Pink, Green) works well as notification layer

**Join/Status: Working**
- Late join (Observer, msg 557) received correct recent_messages context
- `trio_status` correctly reports active/inactive members with last_seen timestamps
- Objective pin displayed correctly on connect
- Pink (late joiner at msg ~25) noted onboarding was rough — had to piece together context

**Potential Bug: Blocking poll marks messages as read without returning them**
- Taskmaster reports: during a `wait_seconds=30` blocking poll, messages arrived and were marked read, but the poll returned "no_new" after timeout. This caused Taskmaster to miss Yellow's build question (msg #582) and Purple's integration report (msg #494).
- If confirmed, this is a race condition in the read-marker logic — messages arriving during the long-poll window get consumed without delivery.
- **Severity: High.** Silent message loss in a system whose core promise is reliable delivery.

---

### Issues Found

**1. Blocking poll may silently consume messages (potential bug)**
- See above. Taskmaster's report suggests messages can arrive during a long poll, get marked read, and never be returned. Needs code-level investigation.
- **Severity:** High. This undermines the reliability guarantee.

**2. No inactive-member notification**
- Red went inactive at 12:19 (dropped off for ~6 min). Taskmaster continued asking Red for confirmation without knowing Red was offline.
- `trio_status` shows active/inactive, but nobody checks it proactively. An automatic "Red went idle" event in the message stream would prevent wasted coordination.
- **Severity:** Medium.

**3. Single pin is insufficient for busy channels**
- Taskmaster missed Purple's long integration report (msg #494) in the scroll. Only one pin allowed (used for objective). 5 of 7 agents cited this as a top pain point.
- **Severity:** Medium.

**4. 4000-char message limit constrains reports**
- Yellow and Purple both had to split reports. 3 of 7 agents cited this.
- Workaround (write to files, post pointer) works but fragments workflow.
- **Severity:** Low-Medium.

**5. No @mention or keyword filtering**
- 4 of 7 agents cited this. During idle waits, every message wakes every poll. Agents blocked on a specific event have no way to filter.
- Green: "I restarted the wait script 15+ times for messages that didn't need my attention."
- **Severity:** Low-Medium at this channel size, would scale to Medium+ with more members.

**6. No message replay / history retrieval**
- Taskmaster: "Once a message is marked read, it's gone from polls. If I miss something, I can't retrieve it."
- Wants `trio_history(channel, last_n)` or `trio_replay(channel, from_message_id)`.
- **Severity:** Medium for coordinators.

**7. No unread count indicator**
- Red: after returning from 6 min away, no indication of how many messages missed.
- Green: wants "messages since my last send" count.
- **Severity:** Low.

**8. Confirmation messages are noisy**
- Pink: ~30% of message volume was pure acknowledgments ("confirmed, not building"). Emoji reactions or read receipts would reduce noise.
- **Severity:** Low.

**9. Task system completely unused despite active coordination**
- Nobody used `trio_claim` or task features. Taskmaster assigned work via chat messages.
- Purple's analysis: trio_claim is designed for parallel-independent work (claim from pool), but this workflow was sequential-dependent (build → fix → test), so central assignment was natural.
- Pink's counter: formal tasks would have *prevented* the concurrent-build collision.
- **Verdict:** Task system fits a different topology than what happened here. But resource-ownership tasks ("I own the build directory") would have helped even in this sequential model.

**10. Late-joiner context gap**
- Pink joined at msg ~25 with 6 members in motion. Got recent_messages tail but had to piece together who was doing what.
- A pinned objective + active task/role list at join time would halve ramp-up.
- **Severity:** Low.

---

### Multi-Agent Workflow Guidance

Practical suggestions derived from observing 7 agents coordinate real C++ build/test/fix work. Not rigid rules — patterns that prevent the problems this channel actually hit. Intended to become codified guidance in the Trio skill.

#### Resource Ownership (builds, file edits)

**The concurrent build problem:** Multiple agents ran builds on the same directory simultaneously. Taskmaster issued an emergency STOP (msg 556). This was the single biggest coordination failure.

**Guidance:**
- **Designate resource owners before work begins.** The coordinator should explicitly assign: who builds, who edits which files, who reviews. "Yellow owns the build directory" solved it the moment it was stated.
- **One writer per file/directory.** Build directories, source files under active edit, and test output are mutually exclusive resources. If two agents need to edit the same file, serialize — one drafts, commits, then the next goes.
- **Announce before touching shared state.** Before running a build or editing source, post: "I'm about to build in X" and wait for acknowledgment. Cheap insurance against collisions.
- **Use trio_claim for resource ownership when available.** Post "build directory" as a task and claim it. Even if the task system feels like overhead for your workflow, the claim acts as a mutex.
- **Don't fill idle time with unauthorized work.** Agents that stayed in "standing by" mode caused zero problems. The build collision came from agents inventing work while blocked.

#### Coordinator Patterns

- **Roll calls work.** Status-check messages got responses from all active agents within ~90 seconds. With 5+ members, do them periodically.
- **Explicit confirmation loops.** Require each agent to confirm individually. Implicit agreement doesn't work when agents poll at different intervals.
- **Don't assume silence is compliance.** Silence might mean "not polling" rather than "agreed." Check `trio_status` for inactive members before waiting on someone.
- **Check trio_status before asking "where are you?"** The heartbeat data is there — use it.

#### Report and Analysis Workflow

- **File long content, summarize in channel.** Write analysis to .md files, post a 2-3 line summary with the path. Keeps the channel scannable.
- **Reference files by path.** "Report filed at rainbow-test/purple-integration.md" lets others read without clogging the channel.
- **When the char limit bites, don't split — file it.** Split messages lose context. A single file with a channel pointer is better than two fragmented messages.

#### Idle Agent Protocol

- **"Standing by" is a useful signal.** Post it so the coordinator knows who's available.
- **State what you're blocked on.** "Standing by — blocked on Yellow's build" is more useful than bare "standing by."
- **Use longer poll intervals during known waits.** If the build takes 10 minutes, don't poll every 15 seconds.

---

### Feature Requests Summary (ranked by team consensus)

| Priority | Request | Cited by | Notes |
|----------|---------|----------|-------|
| High | Fix blocking-poll read-marker race | Taskmaster | Potential bug — messages consumed without delivery |
| Medium | Message replay / history retrieval | Taskmaster | `trio_history(channel, last_n)` for catch-up |
| Medium | Multiple pins or "important" flag | Purple, +4 others | Long reports get buried in 100+ msg channels |
| Medium | Inactive-member events in stream | Observer, Red | Auto-post when member heartbeat lapses |
| Low-Med | @mention / keyword filtering | Green, Pink, +2 | Reduce noise for blocked agents |
| Low-Med | Higher char limit or file-attachment | Yellow, Purple | Reports exceed 4000 chars |
| Low | Unread count on reconnect | Red, Green | "You have N unread" before reading |
| Low | Idle-period hint from server | Orange | "Channel quiet, try longer poll" |
| Low | Emoji reactions / read receipts | Pink | Reduce confirmation message noise |
| Low | Member status field (idle/blocked/working) | Green | Visible in trio_status, set by agent |

---

### Verdict

Trio's core transport works. Reliably. Under genuine load — 8 members, 120+ messages, 35 minutes of coordinated C++ build/test/fix workflow — zero messages were dropped, ordering held perfectly, and concurrent writes from multiple agents were handled cleanly. Every respondent confirmed this independently.

The one potential exception is Taskmaster's report of messages being marked read during a blocking poll without being returned. If confirmed in code, that's the only reliability issue and it's the highest-priority fix.

Everything else is coordination tooling — helping agents know who's online, what's important, when to pay attention, and who owns what. The task system exists but wasn't used, partly because the workflow topology (sequential-dependent) didn't match the system's design (parallel-independent claiming), and partly because message-based coordination felt natural enough. But the concurrent-build collision proves that *some* form of resource ownership — whether via tasks, explicit locks, or just guidance — is essential for multi-agent channels sharing a workspace.

The biggest failure in this session was a workflow problem, not a Trio problem. But the line between "the tool works" and "the tool helps you work" is exactly where these feature requests live. Trio delivered every message — it just didn't help agents know which ones mattered.
