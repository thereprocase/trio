#!/usr/bin/env bash
# Claude Trio — cross-platform setup
# Installs the MCP server and skill for all Claude Code sessions on this machine.
# Works on Linux, macOS, and Windows (Git Bash / MSYS2 / WSL).
#
# What this does:
#   1. Checks for Python 3.10+ and installs the MCP SDK if needed
#   2. Copies skill + server to ~/.claude/skills/trio/
#   3. Registers the MCP server via `claude mcp add` (writes to ~/.claude.json)
#   4. Allowlists trio tools in ~/.claude/settings.json (zero permission prompts)
#
# After setup: restart Claude Code, then /trio works in every session.

set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
SKILL_DIR="${CLAUDE_DIR}/skills/trio"
SERVER_DIR="${SKILL_DIR}/server"
DB_DIR="${CLAUDE_DIR}/trio"

echo "=== Claude Trio Setup ==="
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

# ---------- 3. Copy files ----------

mkdir -p "$SERVER_DIR" "$DB_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/SKILL.md" ]; then
    cp "$SCRIPT_DIR/SKILL.md" "$SKILL_DIR/SKILL.md"
    echo "Skill file: $SKILL_DIR/SKILL.md"
else
    echo "Note: SKILL.md not found in source directory"
fi
cp "$SCRIPT_DIR/server/trio_server.py" "$SERVER_DIR/trio_server.py"
cp "$SCRIPT_DIR/server/trio_wait.py" "$SERVER_DIR/trio_wait.py"
echo "Server files: $SERVER_DIR"

# ---------- 4. Resolve native path ----------
#
# The MCP registration needs the native OS path to trio_server.py.
# In Git Bash on Windows, paths look like /c/Users/... but Claude Code
# needs C:\Users\... to spawn the subprocess correctly.

SERVER_SCRIPT="$SERVER_DIR/trio_server.py"
NATIVE_PATH="$SERVER_SCRIPT"
PLATFORM="unknown"

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        PLATFORM="windows"
        if command -v cygpath &>/dev/null; then
            NATIVE_PATH=$(cygpath -w "$SERVER_SCRIPT")
        else
            NATIVE_PATH=$(echo "$SERVER_SCRIPT" | sed 's|^/\([a-zA-Z]\)/|\1:\\|' | sed 's|/|\\|g')
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

# ---------- 5. Register MCP server ----------
#
# IMPORTANT: MCP servers are registered in ~/.claude.json (the main Claude
# Code config), NOT in ~/.claude/mcp.json. The only reliable way to register
# is via the `claude mcp add` CLI command. Hand-editing config files does not
# work — Claude Code won't see the server.
#
# The -s user flag makes this a user-scoped server (available in all projects).

if command -v claude &>/dev/null; then
    claude mcp remove trio -s user 2>/dev/null || true
    claude mcp add trio -s user -- "$PYTHON_CMD" "$NATIVE_PATH" 2>&1
    echo "MCP server: registered (user scope)"
else
    echo ""
    echo "WARNING: 'claude' CLI not found in PATH."
    echo "After installing Claude Code, register the server manually:"
    echo ""
    echo "  claude mcp add trio -s user -- $PYTHON_CMD \"$NATIVE_PATH\""
    echo ""
    echo "This is the ONLY supported registration method. Do NOT hand-edit"
    echo "~/.claude.json or create ~/.claude/mcp.json — it won't work."
fi

# ---------- 6. Allowlist tools ----------
#
# Add trio MCP tools to the permissions allowlist in ~/.claude/settings.json
# so they run without permission prompts. This file is separate from
# ~/.claude.json and can be safely edited.

SETTINGS_JSON="${CLAUDE_DIR}/settings.json"
TRIO_TOOLS=(
    "mcp__trio__trio_connect"
    "mcp__trio__trio_send"
    "mcp__trio__trio_poll"
    "mcp__trio__trio_claim"
    "mcp__trio__trio_complete"
    "mcp__trio__trio_status"
    "mcp__trio__trio_end"
    "mcp__trio__trio_list"
    "mcp__trio__trio_cleanup"
)

"$PYTHON_CMD" -c "
import json, os

settings_path = '$SETTINGS_JSON'
tools = $(printf '%s\n' "${TRIO_TOOLS[@]}" | "$PYTHON_CMD" -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))")

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

perms = settings.setdefault('permissions', {})
allow = perms.setdefault('allow', [])

added = 0
for tool in tools:
    if tool not in allow:
        allow.append(tool)
        added += 1

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

print(f'Permissions: {added} tool(s) allowlisted ({len(tools) - added} already present)')
"

# ---------- 7. Verify ----------

echo ""
echo "=== Setup Complete ==="
echo ""
echo "  Server:   $NATIVE_PATH"
echo "  Database: $DB_DIR/trio.db (created on first use)"
echo "  Config:   ~/.claude.json (via claude mcp add)"
echo "  Perms:    $SETTINGS_JSON"
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code (exit and re-launch)"
echo "  2. Run /mcp to verify 'trio' appears in the server list"
echo "  3. Try: /trio hello world"
echo ""
echo "Verify with: claude mcp list"
