# Trio Over Tailscale — Remote MCP Plan

**Date:** 2026-04-09
**Status:** Ready to execute
**Estimated time:** ~20 minutes

---

## Problem

Trio's MCP server (`roam-hive-mind`) runs locally via stdio, backed by SQLite at `~/.claude/roam/roam.db`. Every Claude session spawns its own copy. Works great on one machine, doesn't work from the web or another device.

## Solution

FastMCP 1.26.0 already supports SSE transport. Tailscale provides an encrypted WireGuard tunnel with zero port forwarding. Combine them: run one SSE server on the workstation, connect from anywhere on the tailnet.

## Architecture

```
[Local CLI sessions] ──stdio──► [per-session MCP server] ──► roam.db (SQLite WAL)
                                                                  ▲
[Remote sessions] ──SSE/Tailscale──► [single SSE server] ────────┘
```

Local sessions keep stdio (no changes). Remote sessions connect via SSE. Both hit the same database. WAL mode handles concurrent access.

## Steps

### 1. SSE wrapper script (~2 min)

Create `~/.claude/skills/trio/server/roam_hive_mind_sse.py`:

```python
"""SSE transport wrapper for roam-hive-mind MCP server."""
from roam_hive_mind_server import mcp

if __name__ == "__main__":
    mcp.run(transport="sse")
```

Starts uvicorn on port 8000. All 18 tools, same SQLite database, same everything.

### 2. Install Tailscale on this machine (~5 min)

- Download from https://tailscale.com/download/windows
- Install, authenticate with personal account
- Note the assigned IP (e.g., `100.x.y.z`)

### 3. Install Tailscale on remote machine (~5 min)

- Same download/install/auth on the other device
- Verify connectivity: `ping 100.x.y.z`

### 4. Start the SSE server (~1 min)

```bash
cd ~/.claude/skills/trio/server
python roam_hive_mind_sse.py
```

For persistence: run in a terminal, or create a startup script / scheduled task.

### 5. Register remote MCP on the other machine (~1 min)

```bash
claude mcp add roam-hive-mind --transport sse --url http://100.x.y.z:8000/sse
```

### 6. Test (~5 min)

- Local machine: `/trio test-channel hello from local`
- Remote machine: `/trio test-channel hello from remote`
- Verify messages appear in both directions

## What stays the same

- All 18 MCP tools (connect, send, poll, claim, etc.)
- SQLite database location and schema
- Sentinel processes (run on the host machine as usual)
- Skill prompt (SKILL.md) — unchanged
- Local session config — unchanged (stdio)

## What changes

| Component | Before | After |
|---|---|---|
| Remote access | None | SSE over Tailscale |
| Server process | Per-session (stdio) | One persistent SSE + per-session stdio |
| Network exposure | None | Tailnet only (encrypted, no public ports) |
| New files | — | `roam_hive_mind_sse.py` (3 lines) |

## Risks

- **SQLite concurrent writes from two machines:** WAL mode + `busy_timeout=5000` should handle it. Trio's writes are small and fast. If contention appears, bump `busy_timeout` or add retry logic.
- **SSE server goes down:** Remote sessions lose MCP access. Local stdio sessions unaffected. Fix: restart the server. Could add a watchdog later.
- **Tailscale auth expires:** Re-authenticate. Tailscale keys last 180 days by default, or set to never expire in the admin console.

## Future: Serverless upgrade

If this works well and you want to drop the dependency on the home machine being online, swap SQLite for DynamoDB and the SSE server for Lambda + API Gateway. Same MCP tool interface, fully serverless, ~2 days of work. See conversation notes from 2026-04-09.
