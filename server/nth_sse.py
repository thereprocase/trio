"""SSE transport for nth-hive. Run on the hub machine.

Starts an SSE server on 0.0.0.0:8000 that remote Claude sessions
connect to via Tailscale. Same server code, same database, different
transport and MCP name.

Usage:
    python nth_sse.py
    NTH_PORT=9000 python nth_sse.py    # custom port
    NTH_HOST=127.0.0.1 python nth_sse.py  # localhost only
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ["NTH_SERVER_NAME"] = "nth-hive"
os.environ.setdefault("NTH_HOST", "0.0.0.0")
os.environ.setdefault("NTH_PORT", "8000")

from nth_server import mcp

if __name__ == "__main__":
    mcp.run(transport="sse")
