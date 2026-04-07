# Test While Working — Trio v5.2

## When and How to Deploy

**When:** After any code change to sentinel, wrapper scripts, server, or SKILL.md.

**How:**
```bash
# 1. Deploy from the repo
cd D:/ClauDe/tools/trio
bash setup.sh

# 2. Restart Claude Code (required for MCP + schema changes)
# Close and reopen the session, or open a new terminal

# 3. Verify
claude mcp list          # should show roam-hive-mind
/trio --status           # from any session, check it responds
```

**First deploy of v5.2:** The DB migration adds `messenger_heartbeat` and `watchdog_heartbeat` columns. This runs automatically on first `roam_hive_mind_connect` after deploy. No manual migration needed.

## Logging

Log observations to `D:/ClauDe/tools/trio/test-observations.log` as you go. One line per observation, timestamped. The file doesn't need to be pretty — it's raw field notes.

```bash
# Quick log entry from any session
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [channel-name] observation here" >> D:/ClauDe/tools/trio/test-observations.log
```

**What to log:**
- Sentinel launch success/failure and time to `"sentinels": "both"` in status
- Any sentinel nag that appeared (or didn't appear when expected)
- peer_dead events — when, which peer, was it accurate?
- Anything from the "Things That Should Never Happen" list
- Token counts from sentinel agent completions
- Session duration and idle periods

**Example entries:**
```
2026-04-08T14:30:00Z [code-review] Both sentinels up in <10s. Status shows "both".
2026-04-08T15:15:00Z [code-review] Idle 45min, message arrived, sentinel detected in ~3s. No Opus relaunch during idle.
2026-04-08T16:00:00Z [code-review] Closed session B. Session A detected peer_dead after ~6min. Correct.
2026-04-08T16:05:00Z [code-review] BUG: nag appeared in send() response despite both sentinels running.
```

**End of session:** If anything interesting happened, copy relevant entries into a proper report in `reviews/` or file a bug in `bugs/`.

---

Things to watch for during real trio sessions. None of these require dedicated test time — just pay attention while using the system normally.

## Sentinel Startup

**On every `/trio` join, check:**
- Do both sentinels launch without error?
- Does `roam_hive_mind_status` show `"sentinels": "both"` for your member within 30 seconds?
- If a sentinel fails to launch (bash denied, concurrency), does the retry work? Does the surviving sentinel eventually report `peer_dead`?

**First-session-after-restart is critical.** The new heartbeat columns (`messenger_heartbeat`, `watchdog_heartbeat`) require a DB migration. If the migration doesn't fire on the first `roam_hive_mind_connect`, the sentinel will crash trying to read those columns. The wrapper exception handler should catch it and return JSON, but watch for it.

## Sentinel Nags

**Deliberately skip launching sentinels on one session.** Then:
- Send a message. Does the `send()` response include the nag? (`[server] SENTINELS DOWN. You are DEAF. Launch both NOW.`)
- Poll for messages. Does the `poll()` response include the nag?
- Does `roam_hive_mind_status` show `"sentinels": "none"` for that member?
- After launching sentinels, does the nag disappear within one check cycle (~3-30s)?

**Kill one sentinel (not both).** Send a message. Does the response say `[server] {role} sentinel DOWN. Relaunch it.`?

## Restart Loop Durability

**Leave a session idle for 1+ hours on a quiet channel.** Then:
- Does the session respond when a message arrives? (Proves the sentinel survived the idle period.)
- How many restart cycles did the Haiku agent go through? (Check by counting tool calls in the agent output if accessible.)
- Did the Opus parent see any sentinel returns during the idle period? (It shouldn't — restarts are internal to Haiku.)

**Leave a session idle for 3+ hours.** Same checks. This is the MegaSoak production equivalent.

## Peer Heartbeat Detection

**On a 2-session channel, close one session abruptly (Ctrl+C or close the terminal).** Then:
- Does the surviving session's watchdog detect `peer_dead` within ~6 minutes? (5-min threshold + 2 observation confirmation + check interval.)
- Does the Opus parent receive the `peer_dead` event?
- Is the `peer_dead` guidance followed? (Relaunch if idle, defer if active.)

**On a 3+ session channel, close one session.** Do the other sessions' sentinels each independently detect the death? Or does only one fire?

## Cadence Rule Interaction

**During active work on a claimed task:**
- Does the 3-call cadence rule still fire correctly? (Post status every 3 work tool calls.)
- Does the watchdog sentinel detect cadence silence if you go quiet for 10+ minutes?
- After receiving a cadence nag, does posting a status message reset the timer?

## Permission Gate Behavior

**Trigger a permission prompt during active trio work.** (Run a bash command that isn't allowlisted.) Then:
- Does the sentinel keep running while you're gated?
- Does the channel see your "About to run a bash command that may need permission" message?
- When you return from the gate, can you still communicate normally?

## Multi-Channel

**Join two trio channels simultaneously from the same Claude Code session.** (This isn't officially supported but users might try it.)
- Do both channels get sentinels?
- Does the concurrency ceiling cause bash denials?
- Do heartbeats write correctly for both channels?

## Error Recovery

**Corrupt the DB (rename roam.db while a session is active).** Then:
- Does the sentinel's error counter fire after 10 consecutive failures?
- Does the wrapper catch the error and return JSON?
- Does the Haiku agent return the error event to the Opus parent?
- After restoring the DB, does a sentinel relaunch recover cleanly?

## Token Tracking

**On any session that runs for 30+ minutes, note:**
- Total Haiku tokens for sentinel agents (visible in agent completion notifications)
- Number of sentinel returns to Opus parent (should be zero on idle channels, one per message burst on active channels)
- Whether token usage matches the 22K base + 300/restart model from testing

## Things That Should Never Happen

If you see any of these, file a bug:

- **Sentinel returns `peer_dead` within 60 seconds of joining.** (Startup grace period should prevent this.)
- **Both sentinels return simultaneously.** (They watch different events — a simultaneous return means something unexpected fired.)
- **Sentinel returns `restart` event to Opus.** (The wrapper converts these. If Opus sees `restart`, the wrapper is broken.)
- **Haiku agent summarizes or adds commentary to sentinel output.** (Rule 6 says "Do NOT add commentary." If it happens, the prompt needs strengthening.)
- **Sentinel nag appears when both sentinels are running.** (The heartbeat check is broken.)
- **`roam_hive_mind_status` shows `"sentinels": "both"` but nags appear in poll/send.** (The status query and the nag query are using different staleness checks.)

## Low-Priority Observations

Nice to know but not blocking:

- How long does the DB connection survive under sustained multi-agent write load? (WAL growth risk from Gandalf's review.)
- Does the `flag_inconsistency` detection fire correctly in production? (Never been tested outside synthetic scenarios.)
- How does the system behave on Claude Teams (different tier)? Does `timeout: 3600000` work the same?
- What's the actual concurrency ceiling? At what point do new sentinel agent spawns get bash denied?
