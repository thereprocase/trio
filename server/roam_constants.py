# Shared constants for the Trio MCP server and sentinel.
# Both roam_hive_mind_server.py and roam_hive_mind_sentinel.py import from here.
# If you change these, both consumers pick it up automatically.

SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")
