### Frodo — Opus

**Scope:** Trio v5.1 sentinel architecture — operator experience, Opus agent experience, failure modes, test documentation quality.

**Files reviewed:**
- `D:/ClauDe/tools/trio/test-log.md`
- `D:/ClauDe/tools/trio/SKILL.md`
- `D:/ClauDe/tools/trio/server/messenger-foreground.py`
- `D:/ClauDe/tools/trio/server/sentinel-foreground.py`
- `D:/ClauDe/tools/trio/server/roam_hive_mind_sentinel.py`
- `D:/ClauDe/tools/trio/CURRENT.md`

---

**Findings:**

1. **[severity: critical]** `SKILL.md:134,148` — Ghost events in the event tables. SKILL.md documents `sentinel_loop_cap` and `watchdog_loop_cap` events with the description "Sentinel exhausted its 30-hour restart budget. Relaunch it." Neither event is produced by any code. The foreground wrapper scripts convert `cap` to `{"event": "restart"}`, which Haiku loops on silently. There is no `MAX_RESTARTS` counter implemented anywhere. No code path ever emits `sentinel_loop_cap` or `watchdog_loop_cap`. Opus will dutifully handle these events if they arrive — but they never will. Worse: if something goes wrong after 30+ hours and Opus is waiting for a `sentinel_loop_cap` that never fires, there is no fallback. The event table is the contract between the sentinel system and the Opus agent. Ghost entries in that contract are not harmless — they are actively misleading. **Fix:** Either implement MAX_RESTARTS in the foreground wrappers (so the event actually fires after N restarts) or remove both rows from the event tables.

2. **[severity: critical]** `SKILL.md:122` — Contradictory restart visibility claim. Line 122 says: "You will not see cap events — the scripts absorb those silently." But the event tables at lines 134 and 148 list `sentinel_loop_cap` and `watchdog_loop_cap` as events Opus should handle. Opus cannot simultaneously never see cap events and be expected to handle them. This is the kind of contradiction that makes an agent hesitate at the worst possible moment. **Fix:** Decide which is true and make the document say one thing.

3. **[severity: warning]** `CURRENT.md:1` — Version header says "v5.0 RC2" but the body documents v5.1 architecture (foreground wrappers, restart loops, `timeout: 3600000`). Lines 25-26 correctly describe `messenger-foreground.py` and `sentinel-foreground.py` as "(v5.1)" additions. The dual-sentinel monitoring model section (line 30+) documents the v5.1 flow. But the title and version line at the top say v5.0 RC2. Repro opening this file in 6 months will see "v5.0 RC2" and wonder whether v5.1 was ever finished. **Fix:** Update the header to "v5.1" and note v5.0 RC2 as the prior version.

4. **[severity: warning]** `SKILL.md:76-113` — No guidance for what Opus should do if sentinel launch fails. The prompt says "launch BOTH sentinels after connecting" and "BOTH SENTINELS MUST ALWAYS BE RUNNING." But there is zero guidance for what happens when spawning the Haiku agent itself fails. If the Agent tool returns an error (concurrency ceiling, permission denied, rate limit), Opus has no instructions. The test-log documents bash denials under concurrency — this is a real failure mode, not theoretical. **Fix:** Add a brief "If sentinel launch fails" section: retry once, if it fails again tell the user, continue without that sentinel but warn about reduced monitoring.

5. **[severity: warning]** `server/roam_hive_mind_sentinel.py:278-282` — Transient DB errors are silently swallowed. The `except sqlite3.OperationalError` block catches anything that is not "no such table" and does nothing. No log, no counter, no output. If the DB file is locked for 30 seconds straight (another process holding a write lock, disk I/O stall), the sentinel retries silently. Repro has no way to know this is happening. The sentinel could spend its entire 59-minute cycle retrying failed DB queries and appear healthy from the outside. **Fix:** At minimum, count consecutive DB errors and return an error event after N failures (e.g., 10). Something like `{"event": "error", "msg": "DB unreachable after 10 retries"}`.

6. **[severity: warning]** `server/messenger-foreground.py:39-43` and `server/sentinel-foreground.py:33-37` — Error output goes to stdout as JSON, then `sys.exit(1)`. This is correct for the happy path where Haiku reads the JSON. But if the script fails before the `sentinel()` call — e.g., ImportError because `roam_hive_mind_sentinel.py` is missing from the install directory — the Python traceback goes to stderr, nothing goes to stdout, and Haiku sees an empty result from a failed bash command. Haiku's prompt says "read the last JSON line" — there is no JSON line. The prompt has no rule for "script crashed with no output." Haiku will likely fabricate a result (the test-log proved Haiku fabricates on unexpected outcomes). **Fix:** Add rule 6 to the Haiku prompt: "If the command fails with an error (non-zero exit, no JSON output), return the full error output to me immediately."

7. **[severity: warning]** `test-log.md` — The T5 contradiction is left unresolved in a way that could confuse a future reader. The log walks through the heartbeat discovery process chronologically (good for narrative, bad for reference). The final conclusion about idle-output timers vs wall-clock is scattered across three sections: "MAJOR FINDING" (line 272), "Heartbeat600k result" (line 302), and "T5 replay result" (line 326). A reader in 6 months has to synthesize the conclusion from breadcrumbs. The "Recommendations" section at line 369 does consolidate this, but the body of the log leaves the T5 mystery feeling unresolved even though it IS resolved (ratio theory, proven by Heartbeat600k vs T5/T5replay). **Fix:** Add a one-paragraph "T5 resolution" callout near the T5 results that says: "Resolved — see Heartbeat Theory section. The 120s interval was too close to the 600s timeout boundary (5x ratio). 30s intervals (20x ratio) survived. The timeout is idle-output, not wall-clock, and needs >=10x margin."

8. **[severity: note]** `SKILL.md:120` — "Expect long silence" is good guidance but does not tell Opus HOW LONG to expect. "Hours" is vague. The test data shows each cycle is ~59 minutes, and MegaSoak ran 4 hours across 4 restarts with zero returns to Opus. A concrete number like "expect no sentinel returns for 1-4 hours on idle channels" would set better expectations and prevent Opus from deciding something is wrong after 90 minutes of silence.

9. **[severity: note]** `test-log.md:149` — The "Open Questions" section lists questions that are ANSWERED later in the same document (tool call limits answered by ToolLimit50, DB connection by SoakTest). These are not open anymore. A reader scanning for "what still needs testing" will find false leads. **Fix:** Rename to "Questions Investigated" or add a resolved/open tag to each item.

10. **[severity: note]** `test-log.md:237` — "Not yet committed. Repro said skip commits tonight." This is session-specific context that will be confusing once the file IS committed (which it will be). After commit, this line becomes a lie. **Fix:** Remove or update before committing.

11. **[severity: note]** `CURRENT.md:4` — References a `v5.1-sonnet-triage` branch as existing for "future Sonnet triage work." This is stale context from v5.0 RC2. If the branch still exists, fine. If not, this is a dead pointer.

---

**Operator Experience Assessment:**

When Repro runs `/trio` and joins a channel, the sentinel system is designed to be invisible — launch and forget. The SKILL.md prompts are clear enough that Opus should get both sentinels running without intervention. The problem is what happens when things go wrong. There is no feedback path for silent failures. If a sentinel dies (Haiku fabricates, script crashes, DB locks), Repro sees nothing. The dual-sentinel "watch each other" design is sound in theory, but the watchdog checks heartbeat staleness, not whether the messenger sentinel's Haiku agent is alive. If both Haiku agents die (concurrency ceiling), Repro's only signal is that nobody responds to messages — identical to "everyone is busy." There should be a way for Repro to ask "are my sentinels alive?" — even a simple status check that shows when each sentinel last updated the DB.

**Opus Agent Experience Assessment:**

The SKILL.md sentinel prompts (lines 76-113) are well-designed. Five numbered rules, mechanical, no interpretation needed. The 100% foreground compliance rate from testing backs this up. The event tables are the weak point — the ghost events (finding #1) and the contradictory visibility claim (finding #2) could cause Opus to build an incorrect mental model of the system. The "relaunch FIRST, process SECOND" rule is hammered home clearly. The missing error-handling guidance (finding #4) is a gap Opus will hit exactly once before Repro notices, but that once could be during a critical multi-agent session.

**Test Log as Documentation:**

The test-log is excellent as a research journal and good-enough as reference documentation. The chronological structure preserves the discovery process, which is valuable for understanding WHY decisions were made. The Recommendations section at the bottom is the real payoff — well-organized, each bullet backed by specific test names. The main issues are: (a) the Open Questions section contains answered questions (finding #9), (b) the T5 resolution is spread across multiple sections (finding #7), and (c) session-specific notes like "not yet committed" will become stale (finding #10). For a document Repro reads in 6 months, these are speed bumps, not roadblocks.

---

**Summary:** The v5.1 sentinel architecture is well-tested and the wrapper scripts are clean, but SKILL.md contains ghost events and a contradiction that could confuse the Opus agent at exactly the wrong moment.

**Verdict:** ISSUES FOUND (2 critical, 5 warning, 4 note)

---

**Recommendations:**

1. **Reconcile the SKILL.md event tables with reality before committing.** The ghost events are the highest-priority fix. Either implement MAX_RESTARTS or delete the `sentinel_loop_cap`/`watchdog_loop_cap` rows. This is a 5-minute fix either way.

2. **Add a crash-handling rule to the Haiku sentinel prompts.** Rule 6: "If the command fails with an error or produces no JSON output, return the full error to me immediately." This costs nothing and closes the fabrication-on-crash hole.

3. **Add a "sentinel health check" command or status indicator.** Repro should be able to ask "are my sentinels running?" and get an answer that does not require inspecting DB tables. Even a `/trio --sentinel-status` flag that checks the `last_seen` timestamps for the current member would work. This is not urgent, but it is the kind of thing you want before the first time you need it.

4. **Update CURRENT.md to v5.1 before committing.** The version mismatch is the easiest fix on this list and prevents the most common documentation confusion (reading the header and stopping).

5. **Clean up the test-log's "Open Questions" section.** Mark resolved items as resolved. Keep the section for genuinely open items (Teams behavior, multi-channel concurrency ceiling). This turns the document from a journal into a reference.

6. **Consider adding a sentinel startup confirmation.** When the foreground wrapper starts, it could print a single JSON line like `{"event": "started", "script": "messenger-foreground", "pid": 12345}` before entering the loop. This gives Haiku (and by extension Opus) confirmation that the script launched successfully, distinct from "script crashed silently." The idle-output timer would start from this first line of output, buying the full timeout window. Currently the script prints nothing until exit — 59 minutes of silence from the very first moment.
