### Frodo — Opus (v2, post-fix)

**Prior findings status:** Both criticals (ghost events, contradiction) and all 5 warnings confirmed fixed.

**New Findings:**

1. **[severity: critical]** peer_dead missing from watch_events in both wrappers. **STATUS: FIXED** (same as Sauron v2 #2).

2. **[severity: critical]** SELECT missing heartbeat columns. **STATUS: FIXED** (same as Sauron v2 #1).

3. **[severity: warning]** `roam_hive_mind_sentinel.py:149` — `channel_gone` event is undocumented in SKILL.md. Bare return, bypasses should_return. Opus has no guidance. **Fix:** Add to event tables.

4. **[severity: warning]** `CURRENT.md:42-44` — Event table omits peer_dead, error, channel_gone. Disagrees with SKILL.md.

5. **[severity: note]** peer_dead handling guidance in SKILL.md is well-written. Works once plumbing is fixed.

**Verdict:** ISSUES FOUND (2 critical — already fixed, 2 warning, 1 note)
