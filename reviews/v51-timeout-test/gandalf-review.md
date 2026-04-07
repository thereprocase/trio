### Gandalf — Opus

**Scope:** Trio v5.1 sentinel architecture refactor — three-layer design (sentinel.py, wrapper scripts, SKILL.md prompts), timeout/restart mechanics, test methodology, technical debt.

**Files reviewed:**
- `D:/ClauDe/tools/trio/test-log.md` — full test results and recommendations
- `D:/ClauDe/tools/trio/server/roam_hive_mind_sentinel.py` — core sentinel loop (342 lines)
- `D:/ClauDe/tools/trio/server/messenger-foreground.py` — message sentinel wrapper (65 lines)
- `D:/ClauDe/tools/trio/server/sentinel-foreground.py` — watchdog sentinel wrapper (59 lines)
- `D:/ClauDe/tools/trio/SKILL.md` — behavioral layer with agent prompts (620 lines)
- `D:/ClauDe/tools/trio/setup.sh` — deployment script (207 lines)
- `D:/ClauDe/tools/trio/CURRENT.md` — architecture snapshot
- `D:/ClauDe/tools/trio/CHANGELOG.md` — version history

---

**Findings:**

1. **[severity: warning]** `server/messenger-foreground.py:36` / `server/sentinel-foreground.py:30` — **MAX_RUNTIME is hardcoded in two places.** Both wrappers independently define `MAX_RUNTIME = 3540`. If someone changes one and forgets the other, the sentinels drift. The value derives from a contract with the bash timeout (3600000ms - 60s = 3540s), but that contract is documented only in comments. A single source of truth — either a shared constant in `roam_constants.py` or derivation from an environment variable — would eliminate the duplication.

2. **[severity: warning]** `server/roam_hive_mind_sentinel.py:50` — **DEFAULT_MAX_RUNTIME (18000s / 5 hours) is vestigial.** The wrappers override it to 3540s. The CLI interface still uses 18000s as the default. If someone runs the sentinel directly (bypassing wrappers), the 5-hour default will outlive any reasonable bash timeout, and the process will be killed uncleanly. The default should match production reality or be removed in favor of a required parameter.

3. **[severity: warning]** `server/roam_hive_mind_sentinel.py:110` — **Single long-lived SQLite connection for 59 minutes.** The test log (Open Question #4) acknowledges this risk but only validates against a quiet channel. Under sustained multi-agent write load with WAL mode, SQLite connections can accumulate WAL frames that never checkpoint. If the sentinel holds a read transaction open across a check cycle (it does — no explicit transaction boundaries), the WAL file can grow without bound. The `db.commit()` at line 156 helps, but the reads at lines 122-201 are implicit autocommit reads that may or may not hold the WAL open depending on the Python sqlite3 module's transaction behavior. Worth a targeted stress test.

4. **[severity: note]** `server/sentinel-foreground.py:45-48` — **Watchdog hardcodes cadence_threshold=600 and active_interval=30, overriding the defaults.** These overrides are undocumented — the wrapper silently changes behavior from the sentinel's defaults (cadence_threshold=180, active_interval=3). The watchdog intentionally checks less frequently (30s always, 10-minute cadence window), but this is only discoverable by reading the wrapper source. A brief comment explaining the watchdog's different operational profile would help.

5. **[severity: warning]** `server/` — **Seven test scripts in the server directory with no organization.** `test-timeout-ceiling.py`, `test-timeout-unfakeable.py`, `test-timeout-battery.py`, `test-restart-arch.py`, `test-restart-arch-standalone.py`, `test-agent-restart-loop.py`, `test-heartbeat-theory.py` — all live alongside production code. The test log recommends keeping only two. These should move to a `tests/` directory or be pruned. `setup.sh` does not copy them (good), but their presence in the server directory muddies the boundary between production and test artifacts.

6. **[severity: note]** `SKILL.md:76-114` — **The two sentinel prompts are nearly identical.** 11 lines each, differing only in the script path, the description noun, and the opening identity sentence. Fine at two copies, but any expansion would benefit from a template. Not actionable now.

7. **[severity: note]** `server/roam_hive_mind_sentinel.py:259-263` — **Redundant query for latest_own in cadence check.** The same query is executed at line 169 (sleep confirmation) and again at line 259 (cadence check). In active mode the sleep branch is skipped, so no double query in practice. Minor.

8. **[severity: critical]** `server/roam_hive_mind_sentinel.py:217` — **Heartbeat check is a no-op, and dual sentinels cannot detect each other's death.** The sentinel updates `last_seen` at line 152, then checks heartbeat staleness at line 217 by reading `member["last_seen"]` — but `member` was fetched at line 122, BEFORE the update. So `heartbeat_gap` reflects the pre-update value. The comment at lines 220-225 explains this catches silent UPDATE failures or post-restart gaps. However, the deeper problem: both sentinels update the SAME `last_seen` field for the SAME `member_id`. If the message sentinel dies, the watchdog sentinel's heartbeat update keeps `last_seen` fresh, masking the death. The dual-sentinel "watch each other" property claimed in SKILL.md line 116 and CURRENT.md does not actually hold. Both sentinels update the same heartbeat, so neither can detect the other's death.

9. **[severity: warning]** `setup.sh:66` — **Deploys deprecated file.** `roam_hive_mind_wait.py` is copied to the install directory even though it is deprecated per CURRENT.md and CHANGELOG.md. Continuing to deploy it signals to future maintainers that it is still in use.

10. **[severity: note]** `test-log.md` — **Timeout mechanism remains partially unexplained.** The "ratio theory" (need 10x margin between heartbeat interval and timeout) fits two data points but has not been validated at intermediate ratios. The 3600000 silent-sentinel approach sidesteps the question, which is pragmatic. The test log should explicitly flag the ratio theory as unproven.

11. **[severity: note]** `test-log.md` — **No integration test against real trio traffic.** The test log's own TODO #2 calls this out. All tests used synthetic scripts. The sentinel's DB queries, mode detection, and event filtering have not been validated under the v5.1 wrapper pattern with real multi-agent traffic.

---

**On the test methodology:**

The testing was rigorous in its domain. The "unfakeable breadcrumb" technique — embedding `os.urandom(8).hex()` tokens that the model cannot fabricate — is genuinely clever and caught a real fabrication (T2) that looked like a clean pass. The systematic progression from simple duration tests through restart architecture to multi-hour soak tests is well-structured. The discovery that Haiku fabricates plausible output when processes are killed is a significant contribution to the understanding of agent reliability.

The conclusions follow from the evidence with two exceptions:
- The "ratio theory" for heartbeat intervals (Finding 10) is under-determined.
- The dual-sentinel "watch each other" claim (Finding 8) is not supported by the implementation.

The test session's intellectual honesty is notable — contradictions are flagged, not hidden.

---

**Summary:** Sound three-layer architecture with a clean separation of concerns, undermined by one critical flaw (dual sentinels cannot actually detect each other's death) and several maintainability hazards (duplicated constants, test sprawl, vestigial defaults).

**Verdict:** ISSUES FOUND (1 critical, 4 warning, 5 note)

---

## Recommendations

### 1. Fix the dual-sentinel mutual watchdog claim (critical)

Both sentinels update `last_seen` for the same `member_id`. If one dies, the other keeps the heartbeat fresh and nobody notices. Three paths:

**Option A — Separate heartbeat fields.** Each sentinel writes its own timestamp. The watchdog checks the message sentinel's, and vice versa. Requires a schema change.

**Option B — Separate member_ids.** Launch each sentinel with a synthetic member_id. Avoids schema changes but pollutes the member roster.

**Option C — Accept the limitation.** Document that the dual-sentinel pattern provides redundancy (both event types stay covered), not mutual health monitoring. Change the "watch each other" framing in SKILL.md to "cover each other's event types."

Option C is honest and costs nothing. The other options add complexity for a failure mode that may be rare enough to not justify the engineering.

### 2. Extract MAX_RUNTIME to roam_constants.py

One constant, one place to change it. If the adaptive MAX_RUNTIME idea from the test log is pursued, this is where the derivation logic lives.

### 3. Move test scripts to a tests/ directory

Keep `test-restart-arch-standalone.py` and `test-heartbeat-theory.py` as the canonical battery. Archive or delete the rest.

### 4. Remove or gate the sentinel's CLI defaults

The 18000s default is a trap for direct invocation. Either make `max_runtime` required from CLI, or set the CLI default to 3540s.

### 5. Stop deploying roam_hive_mind_wait.py

Remove from `setup.sh` line 66. Add a deprecation notice to the file itself if backward compatibility is needed.

### 6. Run a live integration test before shipping v5.1

The mechanics are proven. The integration is not.

### 7. Document the timeout mechanism honestly

The ratio theory is a useful heuristic, not a proven threshold. Flag it as such.
