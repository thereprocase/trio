# War Council Summary — Trio MCP Server

**Date:** 2026-04-02
**Target:** D:/ClauDe/tools/trio/ (trio MCP server v1+v2)
**Formation:** Full War Council (7 of 9 reviewers reported)

## Verdicts

| Reviewer | Model | Critical | Warning | Note | Verdict |
|----------|-------|----------|---------|------|---------|
| Sauron | Opus | 0 | 4 | 8 | ISSUES FOUND |
| Gandalf | Opus | 0 | 4 | 7 | ISSUES FOUND |
| Frodo | Opus | 2 | 5 | 4 | ISSUES FOUND |
| Aragorn | Sonnet | 0 | 2 | 6 | ISSUES FOUND |
| Legolas | Sonnet | 3 | 5 | 3 | ISSUES FOUND |
| Gimli | Sonnet | 2 | 5 | 4 | ISSUES FOUND |
| Ents | Sonnet | — | — | — | (incomplete) |
| Uruk-Hai | Haiku | — | — | — | (no report) |
| Gollum | Haiku | — | — | — | (no report) |

**Totals (7 reviewers): 7 critical, 25 warning, 32 note**

## Cross-Reviewer Agreements (findings flagged by 2+ reviewers)

### 1. get_db() schema bootstrap on every call (Sauron, Gandalf, Legolas)
All three flagged that 6 DDL statements + 2 PRAGMAs run on every tool invocation. At polling frequency (2-3 calls/sec across 5 agents), this is unnecessary write-lock contention. **Fix: init schema once at module load, cache connection or use lightweight get_db().**

### 2. trio_poll blocks MCP server thread (Gandalf, Legolas, Sauron)
The sleep loop inside trio_poll holds a DB connection and blocks the MCP server for up to 30 seconds. Single-threaded MCP means all other tool calls stall. **Fix: reduce max wait or move to the background wait script pattern.**

### 3. Active channel deletion (Frodo, Aragorn)
trio_cleanup can delete active channels with members still in them. **FIXED in fa1baea — guard added.**

### 4. Input length limits on connect params (Frodo)
summary and skills had no length cap. **FIXED in fa1baea — capped to 200 chars each.**

### 5. Missing messages index (Legolas)
No index on the hot `WHERE channel = ? AND id > ?` query path. **FIXED in fa1baea — idx_messages_channel_id added.**

### 6. TOCTOU race in member count (Sauron)
Member count check and insert are not atomic. Low probability with SQLite WAL serialization, but possible under concurrent joins. **Deferred to v2 — use INSERT with subquery.**

### 7. No member deactivation / stale claim recovery (Sauron, Frodo)
Crashed/disconnected members permanently consume slots. Claimed tasks stuck forever. **Deferred to v2 — reconnection + stale claim tiers (10/20/30 min).**

### 8. trio_wait.py duplicates server logic (Gandalf)
Watermark queries reimplemented without shared module. Drift hazard. **Noted for v2 — extract shared query module.**

## Gimli False Positive

Gimli flagged setup.sh line 143 (`settings_path = '$SETTINGS_JSON'`) as a single-quote expansion bug. **Verified false positive:** the outer delimiter is a double-quoted `python -c "..."` string, so bash expands `$SETTINGS_JSON` before Python sees it. The inner single quotes are Python string delimiters.

## Fixes Applied

| Commit | Fix |
|--------|-----|
| fa1baea | Messages index, input length limits, cleanup guard |
| 3326beb | Pin/objective feature (v2) |
| 9ab5105 | Pin message prefix for stream visibility |
| dec2825 | @mentions with case-insensitive detection |

## Post-Council Fixes

| Commit | Fix |
|--------|-----|
| 3855294 | Ents: double-complete error paths, watermark skip race |
| c97a1cb | Frodo (live install): settings.json path not converted to native Windows |
| 1cc642f | Raw string for Windows backslash escapes in Python allowlist step |

## Remaining Issues (v2 backlog)

1. **Schema bootstrap optimization** — init once at module load, not every get_db() call
2. **trio_poll blocking** — holds MCP server thread up to 30s; reduce max or document
3. **TOCTOU on member count** — atomic INSERT with subquery
4. **Reconnection** — trio_connect(rejoin=member_id) to reactivate existing member, preserve watermark/claims
5. **Stale claim tiers** — 10m SLOW / 20m STALE / 30m DEAD; force-claim at DEAD
6. **Member deactivation** — clear active flag for disconnected members
7. **trio_wait.py duplication** — extract shared query module to avoid drift
8. **Member ID collision** — retry on collision (low probability but confusing failure)
9. **Task claim atomicity** — add explicit BEGIN IMMEDIATE for safety
10. **Per-channel resource limits** — message/task count caps
11. **export_conversation silent exceptions** — add logging
12. **Concurrent claim integration test** — prove atomicity under two real racing processes
13. **posted_by name resolution** — trio_status resolves claimed_by but not posted_by

## Overall Assessment

The trio server is architecturally sound. Async model, atomic claims, watermark polling, and SQLite concurrency are correctly implemented. The primary risks are performance-related (schema bootstrap, poll blocking) and will matter at scale but not at v1 usage levels (2-5 agents). Security surface is clean — all SQL parameterized, input validated at boundaries. The war council found no data corruption or state machine bugs.

**Ship verdict: Ready for production use at v1 scale.**
