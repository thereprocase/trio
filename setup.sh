#!/usr/bin/env bash
# Claude nth — cross-platform setup
# Installs the MCP server and skill for all Claude Code sessions on this machine.
# Works on Linux, macOS, and Windows (Git Bash / MSYS2 / WSL).
#
# Modes:
#   hub    — Full install. Local stdio (nth-cluster) + SSE server for remotes.
#   remote — Skill-only install. Connects to a hub via SSE (nth-hive).
#
# What this does:
#   1. Checks for Python 3.10+ and installs the MCP SDK if needed
#   2. Copies skill + server to ~/.claude/skills/nth/
#   3. Registers the MCP server via `claude mcp add` (writes to ~/.claude.json)
#   4. Allowlists nth tools in ~/.claude/settings.json (zero permission prompts)
#   5. Migrates old roam.db if present
#
# After setup: restart Claude Code, then /nth works in every session.

set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
SKILL_DIR="${CLAUDE_DIR}/skills/nth"
SERVER_DIR="${SKILL_DIR}/server"
DB_DIR="${CLAUDE_DIR}/nth"
OLD_DB_DIR="${CLAUDE_DIR}/roam"

echo "=== Claude nth Setup ==="
echo ""

# ---------- 0. Mode selection ----------

MODE=""
HUB_URL=""

if [ "${1:-}" = "hub" ] || [ "${1:-}" = "remote" ]; then
    MODE="$1"
    shift
else
    echo "Select setup mode:"
    echo "  1) hub    — This machine hosts the database. Local + remote access."
    echo "  2) remote — This machine connects to a hub via Tailscale."
    echo ""
    read -rp "Mode [1/2]: " mode_choice
    case "$mode_choice" in
        1|hub)    MODE="hub" ;;
        2|remote) MODE="remote" ;;
        *)
            echo "ERROR: Invalid choice. Run: bash setup.sh hub  OR  bash setup.sh remote"
            exit 1
            ;;
    esac
fi

if [ "$MODE" = "remote" ]; then
    if [ -n "${1:-}" ]; then
        HUB_URL="$1"
        shift
    else
        echo ""
        read -rp "Hub SSE URL (e.g. http://100.x.y.z:8000/sse): " HUB_URL
    fi
    if [ -z "$HUB_URL" ]; then
        echo "ERROR: Remote mode requires a hub URL."
        exit 1
    fi
fi

echo "Mode: $MODE"
echo ""

# ---------- 1. Python ----------

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: Python not found. Install Python 3.10+ and retry."
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_CMD" --version 2>&1)
echo "Python: $PYTHON_VERSION ($PYTHON_CMD)"

# ---------- 2. MCP SDK ----------

if ! "$PYTHON_CMD" -c "from mcp.server.fastmcp import FastMCP" 2>/dev/null; then
    echo "Installing MCP SDK..."
    "$PYTHON_CMD" -m pip install mcp --quiet
    if ! "$PYTHON_CMD" -c "from mcp.server.fastmcp import FastMCP" 2>/dev/null; then
        echo "ERROR: Failed to install MCP SDK. Run: $PYTHON_CMD -m pip install mcp"
        exit 1
    fi
fi
echo "MCP SDK: OK"

# Hub mode needs uvicorn for SSE transport
if [ "$MODE" = "hub" ]; then
    if ! "$PYTHON_CMD" -c "import uvicorn" 2>/dev/null; then
        echo "Installing uvicorn (SSE transport)..."
        "$PYTHON_CMD" -m pip install uvicorn --quiet
    fi
    echo "uvicorn: OK"
fi

# ---------- 3. Copy files ----------

mkdir -p "$SERVER_DIR" "$DB_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Always copy the skill file
if [ -f "$SCRIPT_DIR/SKILL.md" ]; then
    cp "$SCRIPT_DIR/SKILL.md" "$SKILL_DIR/SKILL.md"
    echo "Skill file: $SKILL_DIR/SKILL.md"
else
    echo "Note: SKILL.md not found in source directory"
fi

if [ "$MODE" = "hub" ]; then
    # Hub: copy all server files
    cp "$SCRIPT_DIR/server/nth_server.py" "$SERVER_DIR/nth_server.py"
    cp "$SCRIPT_DIR/server/nth_sentinel.py" "$SERVER_DIR/nth_sentinel.py"
    cp "$SCRIPT_DIR/server/nth_wait.py" "$SERVER_DIR/nth_wait.py" 2>/dev/null || true
    cp "$SCRIPT_DIR/server/nth_sse.py" "$SERVER_DIR/nth_sse.py"
    cp "$SCRIPT_DIR/server/messenger-foreground.py" "$SERVER_DIR/messenger-foreground.py"
    cp "$SCRIPT_DIR/server/sentinel-foreground.py" "$SERVER_DIR/sentinel-foreground.py"
    cp "$SCRIPT_DIR/server/nth_constants.py" "$SERVER_DIR/nth_constants.py"
    echo "Server files: $SERVER_DIR"
else
    echo "Remote mode: server files not needed (SSE to hub)"
fi

# ---------- 4. Data migration ----------

if [ -f "$OLD_DB_DIR/roam.db" ] && [ ! -f "$DB_DIR/nth.db" ]; then
    cp "$OLD_DB_DIR/roam.db" "$DB_DIR/nth.db"
    echo "Migrated database: roam.db -> nth.db"
fi

# ---------- 5. Resolve native path ----------

if [ "$MODE" = "hub" ]; then
    SERVER_SCRIPT="$SERVER_DIR/nth_server.py"
    NATIVE_PATH="$SERVER_SCRIPT"
fi

PLATFORM="unknown"
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        PLATFORM="windows"
        if [ "$MODE" = "hub" ]; then
            if command -v cygpath &>/dev/null; then
                NATIVE_PATH=$(cygpath -w "$SERVER_SCRIPT")
            else
                NATIVE_PATH=$(echo "$SERVER_SCRIPT" | sed 's|^/\([a-zA-Z]\)/|\1:\\|' | sed 's|/|\\|g')
            fi
        fi
        ;;
    Darwin*)
        PLATFORM="macos"
        ;;
    Linux*)
        PLATFORM="linux"
        ;;
esac
echo "Platform: $PLATFORM"

# ---------- 6. Register MCP server ----------

if command -v claude &>/dev/null; then
    # Clean up old registrations
    claude mcp remove roam-hive-mind -s user 2>/dev/null || true
    claude mcp remove nth-cluster -s user 2>/dev/null || true
    claude mcp remove nth-hive -s user 2>/dev/null || true

    if [ "$MODE" = "hub" ]; then
        claude mcp add nth-cluster -s user -- "$PYTHON_CMD" "$NATIVE_PATH" 2>&1
        echo "MCP server: nth-cluster registered (stdio, user scope)"
    else
        claude mcp add --transport sse -s user nth-hive "$HUB_URL" 2>&1
        echo "MCP server: nth-hive registered (SSE -> $HUB_URL)"
    fi
else
    echo ""
    echo "WARNING: 'claude' CLI not found in PATH."
    if [ "$MODE" = "hub" ]; then
        echo "Register the server manually:"
        echo "  claude mcp add nth-cluster -s user -- $PYTHON_CMD \"$NATIVE_PATH\""
    else
        echo "Register the server manually:"
        echo "  claude mcp add --transport sse -s user nth-hive \"$HUB_URL\""
    fi
fi

# ---------- 7. Allowlist tools ----------

SETTINGS_JSON="${CLAUDE_DIR}/settings.json"
case "$PLATFORM" in
    windows)
        if command -v cygpath &>/dev/null; then
            SETTINGS_JSON=$(cygpath -w "$SETTINGS_JSON")
        else
            SETTINGS_JSON=$(echo "$SETTINGS_JSON" | sed 's|^/\([a-zA-Z]\)/|\1:\\|' | sed 's|/|\\|g')
        fi
        ;;
esac

# MCP prefix depends on mode
if [ "$MODE" = "hub" ]; then
    MCP_PREFIX="nth-cluster"
else
    MCP_PREFIX="nth-hive"
fi

NTH_TOOLS=(
    "mcp__${MCP_PREFIX}__nth_connect"
    "mcp__${MCP_PREFIX}__nth_send"
    "mcp__${MCP_PREFIX}__nth_poll"
    "mcp__${MCP_PREFIX}__nth_ack"
    "mcp__${MCP_PREFIX}__nth_claim"
    "mcp__${MCP_PREFIX}__nth_complete"
    "mcp__${MCP_PREFIX}__nth_cancel"
    "mcp__${MCP_PREFIX}__nth_release"
    "mcp__${MCP_PREFIX}__nth_lock"
    "mcp__${MCP_PREFIX}__nth_unlock"
    "mcp__${MCP_PREFIX}__nth_set_status"
    "mcp__${MCP_PREFIX}__nth_status"
    "mcp__${MCP_PREFIX}__nth_roster"
    "mcp__${MCP_PREFIX}__nth_history"
    "mcp__${MCP_PREFIX}__nth_end"
    "mcp__${MCP_PREFIX}__nth_list"
    "mcp__${MCP_PREFIX}__nth_cull"
    "mcp__${MCP_PREFIX}__nth_cleanup"
)

# Also remove old roam-hive-mind tool permissions
OLD_TOOLS_PATTERN="roam-hive-mind"

"$PYTHON_CMD" -c "
import json, os

settings_path = r'$SETTINGS_JSON'
tools = $(printf '%s\n' "${NTH_TOOLS[@]}" | "$PYTHON_CMD" -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))")
old_pattern = '$OLD_TOOLS_PATTERN'

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

perms = settings.setdefault('permissions', {})
allow = perms.setdefault('allow', [])

# Remove old roam-hive-mind entries
removed = [t for t in allow if old_pattern in t]
allow[:] = [t for t in allow if old_pattern not in t]

added = 0
for tool in tools:
    if tool not in allow:
        allow.append(tool)
        added += 1

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

print(f'Permissions: {added} tool(s) allowlisted, {len(removed)} old entries removed')
"

# ---------- 8. Verify ----------

echo ""
echo "=== Setup Complete ($MODE mode) ==="
echo ""
if [ "$MODE" = "hub" ]; then
    echo "  MCP:      nth-cluster (stdio, local)"
    echo "  Server:   $NATIVE_PATH"
    echo "  Database: $DB_DIR/nth.db (created on first use)"
    echo "  Config:   ~/.claude.json (via claude mcp add)"
    echo "  Perms:    $SETTINGS_JSON"
    echo ""
    echo "  To serve remote sessions:"
    echo "    python $SERVER_DIR/nth_sse.py"
    echo "  (Starts SSE server on 0.0.0.0:8000 — accessible via Tailscale)"
else
    echo "  MCP:      nth-hive (SSE -> $HUB_URL)"
    echo "  Config:   ~/.claude.json (via claude mcp add)"
    echo "  Perms:    $SETTINGS_JSON"
fi
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code (exit and re-launch)"
echo "  2. Run /mcp to verify nth tools appear"
echo "  3. Try: /nth hello world"
echo ""
echo "Verify with: claude mcp list"
