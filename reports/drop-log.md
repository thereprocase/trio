# Drop log — transient connectivity events

One line per observed blink; correlate before pathologizing. Pattern of
interest: failures NOT correlated with a hub deploy/restart.

| when (EDT) | observer | symptom | verdict |
|---|---|---|---|
| 2026-08-11 ~11:05 | winvol-listener spoke monitor | SSE EOF, poll errors, self-healed | hub restart (v7.3.1 deploy); clean EOF path |
| 2026-08-11 ~12:55 | winvol-listener spoke monitor | wedged "no SSE endpoint" until killed | hub restart without FIN; FIXED — 90s read timeout + force_reconnect (420ea50) |
| 2026-08-11 ~13:52 | winvol-cachy MCP client | 2 consecutive MCP handshake failures, /sse HTTP 200 throughout, 3rd attempt OK | correlated: context-relay hub deploy restart window; auto-reinit recovered. First MCP-layer blink observed — watch for uncorrelated recurrence |
| 2026-08-11 ~14:0x | Codex-Terra monitor (all restarts) | "connect failed: Timed out waiting for SSE endpoint event", repeatedly, while identical monitors connected fine | NOT a hub fault. Codex session sandbox flipped network_access true→false at 17:08Z when the auto-approve permission profile was selected (verified in rollout turn_context); every codex-spawned process is network-blind. Fix = restore network in codex's active profile or run its monitor outside codex |
