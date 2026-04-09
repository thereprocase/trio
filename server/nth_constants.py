# Shared constants for the nth MCP server and sentinel.
# Both nth_server.py and nth_sentinel.py import from here.
# If you change these, both consumers pick it up automatically.

SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")

# Sentinel wrapper timing. BASH_TIMEOUT_MS is the timeout passed to the
# Bash tool inside the Haiku agent prompt. MAX_RUNTIME_S is how long the
# sentinel script runs before exiting with a "restart" event. The gap
# (60s) ensures the script always exits cleanly before the bash idle-output
# timer kills it. Worst-case loop overrun (~35s) means real margin is ~26s.
BASH_TIMEOUT_MS = 3600000
MAX_RUNTIME_S = 3540
