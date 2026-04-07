### Sauron — Opus (v2 post-fix review)

**Scope:** Trio v5.1 sentinel after applying fixes from v1 review.
**Files:** `server/roam_hive_mind_sentinel.py`, `server/roam_constants.py`, `server/messenger-foreground.py`, `server/sentinel-foreground.py`, `server/roam_hive_mind_server.py` (migration block)

---

**Findings:**

1. **[severity: critical]** `roam_hive_mind_sentinel.py:133-137` — Peer heartbeat column is never SELECTed from DB. The member query fetches only `last_seen, last_read, status_text, status_changed_at`. At line 301, `peer_col in member.keys()` is always False, so `peer_hb` is always `None`, so `seconds_since(None)` returns `inf`, so `peer_dead_streak` increments every cycle. After 2 cycles (6s in active mode), the sentinel returns a false-positive `peer_dead` event — unless the watch filter suppresses it (see finding 2). **Fix:** Add `messenger_heartbeat, watchdog_heartbeat` to the SELECT at line 134. **STATUS: FIXED.**

2. **[severity: warning]** `messenger-foreground.py:50`, `sentinel-foreground.py:47` — Neither foreground wrapper includes `"peer_dead"` in its `watch_events` list. This means `should_return("peer_dead")` is always False, the event is swallowed, and the loop continues. Combined with finding 1, the peer heartbeat feature is entirely inert. **Fix:** Add `"peer_dead"` to `watch_events` in both wrappers. **STATUS: FIXED.**

3. **[severity: warning]** `roam_hive_mind_sentinel.py:301-303` — Dead try/except around `member[peer_col]`. The `if peer_col in member.keys()` guard short-circuits first. **Fix:** Remove try/except. **STATUS: FIXED.**

4. **[severity: note]** `roam_hive_mind_sentinel.py:86` — No CLI path for `--role`. Only set by wrappers. Fine if CLI use is deprecated.

5. **[severity: note]** `roam_hive_mind_sentinel.py:164-175` — Both sentinels race on `last_seen` column. Harmless now but could mask dead parent if `last_seen` is used for parent liveness.

---

**Summary:** Peer heartbeat was wired on write side but broken on read side (missing SELECT columns + missing watch filter). Three fixes applied immediately. DB error counter and prev_msg_count fixes are clean.

**Verdict:** ISSUES FOUND (1 critical — fixed immediately, 2 warning — fixed immediately, 2 note)
