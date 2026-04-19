#!/usr/bin/env bash
# Claude nth — cross-platform setup
# Installs the MCP server and skills for all Claude Code sessions on this machine.
# Works on Linux, macOS, and Windows (Git Bash / MSYS2 / WSL).
#
# Modes:
#   hub    — Full install. /trio (local stdio) + /quartet (SSE for remotes).
#            Registers nth-trio (stdio) + nth-qweb (SSE). Runs quartet_server.py.
#   remote — Remote install. /trio (local stdio) + /quartet (SSE to hub).
#            Registers nth-trio (stdio) + nth-qweb (SSE pointing at hub).
#
# Both modes get /trio for local use. Hub also serves /quartet for remotes.
#
# After setup: restart Claude Code, then /trio and /quartet work.

set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
TRIO_SKILL_DIR="${CLAUDE_DIR}/skills/trio"
QUARTET_SKILL_DIR="${CLAUDE_DIR}/skills/quartet"
SERVER_DIR="${CLAUDE_DIR}/skills/nth/server"
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
    echo "  1) hub    — This machine hosts the DB + serves remotes via Tailscale."
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

mkdir -p "$TRIO_SKILL_DIR" "$QUARTET_SKILL_DIR" "$SERVER_DIR" "$DB_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Each skill gets its own directory with a SKILL.md plus companion docs.
# Companion files (REFERENCE, PROTOCOLS) are per-flavor; DESIGN is shared.
if [ -f "$SCRIPT_DIR/SKILL-trio.md" ]; then
    cp "$SCRIPT_DIR/SKILL-trio.md" "$TRIO_SKILL_DIR/SKILL.md"
    [ -f "$SCRIPT_DIR/REFERENCE-trio.md" ] && cp "$SCRIPT_DIR/REFERENCE-trio.md" "$TRIO_SKILL_DIR/REFERENCE.md"
    [ -f "$SCRIPT_DIR/PROTOCOLS-trio.md" ] && cp "$SCRIPT_DIR/PROTOCOLS-trio.md" "$TRIO_SKILL_DIR/PROTOCOLS.md"
    [ -f "$SCRIPT_DIR/DESIGN.md" ] && cp "$SCRIPT_DIR/DESIGN.md" "$TRIO_SKILL_DIR/DESIGN.md"
fi
if [ -f "$SCRIPT_DIR/SKILL-quartet.md" ]; then
    cp "$SCRIPT_DIR/SKILL-quartet.md" "$QUARTET_SKILL_DIR/SKILL.md"
    [ -f "$SCRIPT_DIR/REFERENCE-quartet.md" ] && cp "$SCRIPT_DIR/REFERENCE-quartet.md" "$QUARTET_SKILL_DIR/REFERENCE.md"
    [ -f "$SCRIPT_DIR/PROTOCOLS-quartet.md" ] && cp "$SCRIPT_DIR/PROTOCOLS-quartet.md" "$QUARTET_SKILL_DIR/PROTOCOLS.md"
    [ -f "$SCRIPT_DIR/DESIGN.md" ] && cp "$SCRIPT_DIR/DESIGN.md" "$QUARTET_SKILL_DIR/DESIGN.md"
fi
# Remove old single-skill install
rm -f "${CLAUDE_DIR}/skills/nth/SKILL.md" 2>/dev/null || true
rm -f "${CLAUDE_DIR}/skills/nth/SKILL-trio.md" 2>/dev/null || true
rm -f "${CLAUDE_DIR}/skills/nth/SKILL-quartet.md" 2>/dev/null || true
echo "Skills: /trio -> $TRIO_SKILL_DIR, /quartet -> $QUARTET_SKILL_DIR"

# Copy server files (both modes need them for local /trio)
cp "$SCRIPT_DIR/server/nth_server.py" "$SERVER_DIR/nth_server.py"
cp "$SCRIPT_DIR/server/nth_monitor.py" "$SERVER_DIR/nth_monitor.py"
cp "$SCRIPT_DIR/server/quartet_server.py" "$SERVER_DIR/quartet_server.py"
cp "$SCRIPT_DIR/server/nth_constants.py" "$SERVER_DIR/nth_constants.py"

# Clean up deprecated files from earlier Haiku-subagent design
rm -f "$SERVER_DIR/nth_sentinel.py" \
      "$SERVER_DIR/nth_wait.py" \
      "$SERVER_DIR/messenger-foreground.py" \
      "$SERVER_DIR/sentinel-foreground.py" 2>/dev/null || true
rm -f "${CLAUDE_DIR}/agents/trio-sentinel.md" 2>/dev/null || true

echo "Server files: $SERVER_DIR"

# ---------- 4. Data migration ----------

if [ -f "$OLD_DB_DIR/roam.db" ] && [ ! -f "$DB_DIR/nth.db" ]; then
    cp "$OLD_DB_DIR/roam.db" "$DB_DIR/nth.db"
    echo "Migrated database: roam.db -> nth.db"
fi

# ---------- 5. Resolve native path ----------

SERVER_SCRIPT="$SERVER_DIR/nth_server.py"
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

# ---------- 6. Register MCP servers ----------

if command -v claude &>/dev/null; then
    # Clean up old registrations
    claude mcp remove roam-hive-mind -s user 2>/dev/null || true
    claude mcp remove nth-cluster -s user 2>/dev/null || true
    claude mcp remove nth-hive -s user 2>/dev/null || true
    claude mcp remove nth-trio -s user 2>/dev/null || true
    claude mcp remove nth-qweb -s user 2>/dev/null || true

    # Both modes: register nth-trio (local stdio) — /trio always works
    claude mcp add nth-trio -s user -- "$PYTHON_CMD" "$NATIVE_PATH" 2>&1
    echo "MCP server: nth-trio registered (stdio, /trio)"

    # Remote mode: also register nth-qweb (SSE to hub) — /quartet connects to hub
    if [ "$MODE" = "remote" ]; then
        claude mcp add --transport sse -s user nth-qweb "$HUB_URL" 2>&1
        echo "MCP server: nth-qweb registered (SSE -> $HUB_URL, /quartet)"
    fi
else
    echo ""
    echo "WARNING: 'claude' CLI not found in PATH."
    echo "Register manually:"
    echo "  claude mcp add nth-trio -s user -- $PYTHON_CMD \"$NATIVE_PATH\""
    if [ "$MODE" = "remote" ]; then
        echo "  claude mcp add --transport sse -s user nth-qweb \"$HUB_URL\""
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

# Tool base names (18 tools)
TOOL_BASES=(connect send poll ack claim complete cancel release lock unlock set_status status roster history end list cull cleanup)

# Build allowlist arrays
TRIO_TOOLS=()
QUARTET_TOOLS=()
for base in "${TOOL_BASES[@]}"; do
    TRIO_TOOLS+=("mcp__nth-trio__trio_${base}")
    QUARTET_TOOLS+=("mcp__nth-qweb__quartet_${base}")
done

# Combine based on mode
if [ "$MODE" = "hub" ]; then
    # Hub: allowlist trio tools only (quartet served, not consumed locally)
    ALL_TOOLS=("${TRIO_TOOLS[@]}")
else
    # Remote: allowlist both trio (local) and quartet (to hub)
    ALL_TOOLS=("${TRIO_TOOLS[@]}" "${QUARTET_TOOLS[@]}")
fi

# Patterns to clean up
OLD_PATTERNS="roam-hive-mind nth-cluster nth-hive"

"$PYTHON_CMD" -c "
import json, os

settings_path = r'$SETTINGS_JSON'
tools = $(printf '%s\n' "${ALL_TOOLS[@]}" | "$PYTHON_CMD" -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))")
old_patterns = '$OLD_PATTERNS'.split()

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

perms = settings.setdefault('permissions', {})
allow = perms.setdefault('allow', [])

# Remove old entries
removed = [t for t in allow if any(p in t for p in old_patterns)]
allow[:] = [t for t in allow if not any(p in t for p in old_patterns)]

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
echo "  /trio:    nth-trio (local stdio, always works)"
if [ "$MODE" = "hub" ]; then
    echo "  /quartet: Start quartet_server.py to serve remotes"
    echo ""
    echo "  Server:   $NATIVE_PATH"
    echo "  Database: $DB_DIR/nth.db (created on first use)"
    echo ""
    echo "  To serve remote /quartet sessions:"
    echo "    python $SERVER_DIR/quartet_server.py"
    echo "  (SSE on 0.0.0.0:8000 — accessible via Tailscale)"
else
    echo "  /quartet: nth-qweb (SSE -> $HUB_URL)"
fi
echo ""
echo "  Config: ~/.claude.json (via claude mcp add)"
echo "  Perms:  $SETTINGS_JSON"
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code (exit and re-launch)"
echo "  2. Run /mcp to verify trio + quartet tools appear"
echo "  3. Try: /trio hello world"
echo ""
echo "Verify with: claude mcp list"
