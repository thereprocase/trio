"""SSE transport for nth-qweb (/quartet). Run on the hub machine.

Starts an SSE server on 0.0.0.0:8000 that remote Claude sessions
connect to via Tailscale. Same server code, same database, different
transport and tool prefix.

Usage:
    python quartet_server.py
    NTH_PORT=9000 python quartet_server.py    # custom port
    NTH_HOST=127.0.0.1 python quartet_server.py  # localhost only
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ["NTH_SERVER_NAME"] = "nth-qweb"
os.environ["NTH_TOOL_PREFIX"] = "quartet"
os.environ.setdefault("NTH_HOST", "0.0.0.0")
os.environ.setdefault("NTH_PORT", "8000")

from nth_server import mcp

if __name__ == "__main__":
    mcp.run(transport="sse")
