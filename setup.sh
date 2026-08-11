#!/usr/bin/env bash
# Claude nth — cross-platform setup
# Installs the MCP server and skills for all Claude Code sessions on this machine.
# Works on Linux, macOS, and Windows (Git Bash / MSYS2 / WSL).
#
# Modes:
#   hub   — Full install. /trio (local stdio) + /quartet (SSE for spokes).
#           Registers nth-trio (stdio) + serves nth-qweb. Runs quartet_server.py.
#   spoke — Spoke install (formerly "remote"; that name still works as an alias).
#           /trio (local stdio) + /quartet (SSE to hub).
#           Registers nth-trio (stdio) + nth-qweb (SSE pointing at hub).
#
# Both modes get /trio for local use. Hub also serves /quartet for spokes.
#
# After setup: restart Claude Code, then /trio and /quartet work.

set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
TRIO_SKILL_DIR="${CLAUDE_DIR}/skills/trio"
QUARTET_SKILL_DIR="${CLAUDE_DIR}/skills/quartet"
SERVER_DIR="${CLAUDE_DIR}/skills/nth/server"
DB_DIR="${CLAUDE_DIR}/nth"
OLD_DB_DIR="${CLAUDE_DIR}/roam"
VENV_DIR="${DB_DIR}/venv"

echo "=== Claude nth Setup ==="
echo ""

# ---------- hub-service mode (root + systemd; the persistent hub box) ----------
# bash setup.sh hub-service     first install AND upgrade (alias: upgrade)
#
# Owns the whole hub deployment so it can never drift from the repo again:
# repo -> /opt/quartet-hub (with .bak-YYYYMMDD backups), a dedicated venv,
# and canonical systemd units for quartet-hub (SSE MCP, :8000) and nth-web
# (landing page + channel dashboards, :8765). Compile + import checks run
# BEFORE the restart so a bad deploy never takes the hub down.

if [ "${1:-}" = "hub-service" ] || [ "${1:-}" = "upgrade" ]; then
    HUB_DIR="/opt/quartet-hub"
    HUB_HOME="/var/lib/quartet-hub"
    STAMP="$(date +%Y%m%d)"
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [ "$(id -u)" != "0" ]; then
        echo "ERROR: hub-service mode must run as root (owns /opt + systemd units)."
        exit 1
    fi
    if ! command -v systemctl &>/dev/null; then
        echo "ERROR: hub-service mode requires systemd."
        exit 1
    fi

    mkdir -p "$HUB_DIR" "$HUB_HOME"

    echo "Deploying server files: repo -> $HUB_DIR (backups: *.bak-$STAMP)"
    for f in nth_server.py nth_monitor.py nth_console.py nth_dashboard.py \
             nth_web.py quartet_server.py nth_constants.py nth_doctor.py; do
        if [ -f "$HUB_DIR/$f" ] && ! cmp -s "$SCRIPT_DIR/server/$f" "$HUB_DIR/$f"; then
            cp "$HUB_DIR/$f" "$HUB_DIR/$f.bak-$STAMP"
        fi
        cp "$SCRIPT_DIR/server/$f" "$HUB_DIR/$f"
    done

    # Dedicated venv — same rationale and pin as spoke mode (mcp 2.0 removed
    # FastMCP; OS python upgrades orphan site-packages). Wheels only.
    HUB_VENV="$HUB_DIR/venv"
    if ! "$HUB_VENV/bin/python" -c "import sys" &>/dev/null; then
        echo "Creating hub venv: $HUB_VENV"
        rm -rf "$HUB_VENV"
        python3 -m venv "$HUB_VENV"
    fi
    if ! "$HUB_VENV/bin/python" -c "from mcp.server.fastmcp import FastMCP; import uvicorn" &>/dev/null; then
        echo "Installing into hub venv: mcp<2 uvicorn"
        "$HUB_VENV/bin/python" -m pip install --quiet --upgrade pip
        "$HUB_VENV/bin/python" -m pip install --quiet --only-binary :all: "mcp<2" uvicorn
    fi
    if ! "$HUB_VENV/bin/python" -c "from mcp.server.fastmcp import FastMCP; import uvicorn" &>/dev/null; then
        echo "ERROR: hub venv cannot import FastMCP + uvicorn. Hub NOT restarted."
        exit 1
    fi

    echo "Compile check..."
    if ! "$HUB_VENV/bin/python" -m py_compile "$HUB_DIR"/*.py; then
        echo "ERROR: py_compile failed. Hub NOT restarted (old process still serving)."
        exit 1
    fi

    cat > /etc/systemd/system/quartet-hub.service <<UNIT
# Managed by trio/setup.sh hub-service — edit the repo, not this file.
[Unit]
Description=nth quartet hub (SSE MCP server for /quartet spokes)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# De-rooted HOME keeps the shared DB out of /root: everything lives under
# ${HUB_HOME}/.claude/nth/ and survives reinstalls via StateDirectory.
Environment=HOME=${HUB_HOME}
StateDirectory=quartet-hub
WorkingDirectory=${HUB_DIR}
ExecStart=${HUB_VENV}/bin/python ${HUB_DIR}/quartet_server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

    cat > /etc/systemd/system/nth-web.service <<UNIT
# Managed by trio/setup.sh hub-service — edit the repo, not this file.
[Unit]
Description=nth web landing page (fleet health + channel dashboards)
After=network-online.target quartet-hub.service

[Service]
Type=simple
Environment=HOME=${HUB_HOME}
WorkingDirectory=${HUB_DIR}
ExecStart=${HUB_VENV}/bin/python ${HUB_DIR}/nth_web.py --tailnet --port 8765
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

    # The unit files written above are canonical now — retire drop-ins from
    # the hand-managed era so there is exactly one source of ExecStart truth.
    rm -rf /etc/systemd/system/quartet-hub.service.d

    systemctl daemon-reload
    systemctl enable quartet-hub.service nth-web.service >/dev/null 2>&1 || true
    echo "Restarting services..."
    systemctl restart quartet-hub.service
    systemctl restart nth-web.service

    sleep 2
    echo ""
    if curl -fsS -m 5 http://127.0.0.1:8000/healthz; then
        echo ""
        echo "quartet-hub: /healthz OK"
    else
        echo "WARNING: /healthz not answering yet — check: journalctl -u quartet-hub -n 30"
    fi
    if curl -fsS -m 5 -o /dev/null http://127.0.0.1:8765/; then
        echo "nth-web:     landing page OK (port 8765)"
    else
        echo "WARNING: nth-web not answering — check: journalctl -u nth-web -n 30"
    fi
    echo ""
    echo "=== Hub service deploy complete ==="
    exit 0
fi

# ---------- 0. Mode selection ----------

MODE=""
HUB_URL=""

case "${1:-}" in
    hub)          MODE="hub";   shift ;;
    spoke|remote) MODE="spoke"; shift ;;
esac

if [ -z "$MODE" ]; then
    echo "Select setup mode:"
    echo "  1) hub   — This machine hosts the DB + serves spokes via Tailscale."
    echo "  2) spoke — This machine connects to a hub via Tailscale."
    echo ""
    read -rp "Mode [1/2]: " mode_choice
    case "$mode_choice" in
        1|hub)          MODE="hub" ;;
        2|spoke|remote) MODE="spoke" ;;
        *)
            echo "ERROR: Invalid choice. Run: bash setup.sh hub  OR  bash setup.sh spoke"
            exit 1
            ;;
    esac
fi

if [ "$MODE" = "spoke" ]; then
    if [ -n "${1:-}" ]; then
        HUB_URL="$1"
        shift
    else
        echo ""
        read -rp "Hub SSE URL (e.g. http://100.x.y.z:8000/sse): " HUB_URL
    fi
    if [ -z "$HUB_URL" ]; then
        echo "ERROR: Spoke mode requires a hub URL."
        exit 1
    fi
fi

echo "Mode: $MODE"
echo ""

# ---------- 1. Python + platform ----------

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

PLATFORM="unknown"
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
    Darwin*)              PLATFORM="macos" ;;
    Linux*)               PLATFORM="linux" ;;
esac
echo "Platform: $PLATFORM"

# ---------- 2. Dedicated venv (MCP SDK) ----------
# The server runs from its own venv, NOT the OS python. Rationale: an OS
# python minor-version bump orphans user-site packages (Arch 3.12 -> 3.14
# silently killed a spoke's stdio registration this way), and PEP 668
# blocks bare `pip install` into system pythons on modern distros anyway.
# The venv is keyed to the DB dir so one machine has exactly one of them.
# Wheels only — no sdist build steps, no lifecycle scripts.

if [ "$PLATFORM" = "windows" ]; then
    VENV_PY="$VENV_DIR/Scripts/python.exe"
else
    VENV_PY="$VENV_DIR/bin/python"
fi

mkdir -p "$DB_DIR"

# Rebuild the venv if its interpreter is missing OR broken (a dangling
# symlink to a removed OS python is exactly the failure this fixes).
if ! "$VENV_PY" -c "import sys" &>/dev/null; then
    if [ -d "$VENV_DIR" ]; then
        echo "venv interpreter broken (OS python upgrade?) — rebuilding $VENV_DIR"
        rm -rf "$VENV_DIR"
    else
        echo "Creating venv: $VENV_DIR"
    fi
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

# Pin mcp to 1.x: SDK 2.0.0 removed mcp.server.fastmcp (FastMCP), which the
# entire server is built on, and quartet_server.py patches 1.x internals.
VENV_PKGS=("mcp<2")
if [ "$MODE" = "hub" ]; then
    VENV_PKGS+=(uvicorn)
fi

NEED_INSTALL=0
"$VENV_PY" -c "from mcp.server.fastmcp import FastMCP" &>/dev/null || NEED_INSTALL=1
if [ "$MODE" = "hub" ]; then
    "$VENV_PY" -c "import uvicorn" &>/dev/null || NEED_INSTALL=1
fi

if [ "$NEED_INSTALL" = "1" ]; then
    echo "Installing into venv: ${VENV_PKGS[*]}"
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet --only-binary :all: "${VENV_PKGS[@]}"
fi

if ! "$VENV_PY" -c "from mcp.server.fastmcp import FastMCP" &>/dev/null; then
    echo "ERROR: venv python cannot import FastMCP after install."
    echo "Debug: $VENV_PY -m pip install mcp"
    exit 1
fi
echo "MCP SDK: OK ($VENV_PY)"

if [ "$MODE" = "hub" ]; then
    if ! "$VENV_PY" -c "import uvicorn" &>/dev/null; then
        echo "ERROR: venv python cannot import uvicorn (needed for SSE transport)."
        exit 1
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
cp "$SCRIPT_DIR/server/nth_console.py" "$SERVER_DIR/nth_console.py"
cp "$SCRIPT_DIR/server/nth_dashboard.py" "$SERVER_DIR/nth_dashboard.py"
cp "$SCRIPT_DIR/server/nth_web.py" "$SERVER_DIR/nth_web.py"
cp "$SCRIPT_DIR/server/quartet_server.py" "$SERVER_DIR/quartet_server.py"
cp "$SCRIPT_DIR/server/nth_constants.py" "$SERVER_DIR/nth_constants.py"
cp "$SCRIPT_DIR/server/nth_doctor.py" "$SERVER_DIR/nth_doctor.py"

# nth-doctor launcher: stdlib-only health check, callable from anywhere.
if [ "$PLATFORM" != "windows" ]; then
    mkdir -p "${HOME}/.local/bin"
    cat > "${HOME}/.local/bin/nth-doctor" <<'LAUNCHER'
#!/usr/bin/env bash
exec python3 "$HOME/.claude/skills/nth/server/nth_doctor.py" "$@"
LAUNCHER
    chmod +x "${HOME}/.local/bin/nth-doctor"
    echo "Doctor: nth-doctor -> ~/.local/bin/nth-doctor"
fi

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

# ---------- 5. Resolve native paths ----------

SERVER_SCRIPT="$SERVER_DIR/nth_server.py"
NATIVE_PATH="$SERVER_SCRIPT"
NATIVE_VENV_PY="$VENV_PY"

if [ "$PLATFORM" = "windows" ]; then
    if command -v cygpath &>/dev/null; then
        NATIVE_PATH=$(cygpath -w "$SERVER_SCRIPT")
        NATIVE_VENV_PY=$(cygpath -w "$VENV_PY")
    else
        NATIVE_PATH=$(echo "$SERVER_SCRIPT" | sed 's|^/\([a-zA-Z]\)/|\1:\\|' | sed 's|/|\\|g')
        NATIVE_VENV_PY=$(echo "$VENV_PY" | sed 's|^/\([a-zA-Z]\)/|\1:\\|' | sed 's|/|\\|g')
    fi
fi

# ---------- 6. Register MCP servers ----------

if command -v claude &>/dev/null; then
    # Clean up old registrations
    claude mcp remove roam-hive-mind -s user 2>/dev/null || true
    claude mcp remove nth-cluster -s user 2>/dev/null || true
    claude mcp remove nth-hive -s user 2>/dev/null || true
    claude mcp remove nth-trio -s user 2>/dev/null || true
    claude mcp remove nth-qweb -s user 2>/dev/null || true

    # Both modes: register nth-trio (local stdio) against the VENV python —
    # /trio always works and survives OS python upgrades.
    claude mcp add nth-trio -s user -- "$NATIVE_VENV_PY" "$NATIVE_PATH" 2>&1
    echo "MCP server: nth-trio registered (stdio, /trio, venv python)"

    # Spoke mode: also register nth-qweb (SSE to hub) — /quartet connects to hub
    if [ "$MODE" = "spoke" ]; then
        claude mcp add --transport sse -s user nth-qweb "$HUB_URL" 2>&1
        echo "MCP server: nth-qweb registered (SSE -> $HUB_URL, /quartet)"
    fi
else
    echo ""
    echo "WARNING: 'claude' CLI not found in PATH."
    echo "Register manually:"
    echo "  claude mcp add nth-trio -s user -- \"$NATIVE_VENV_PY\" \"$NATIVE_PATH\""
    if [ "$MODE" = "spoke" ]; then
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

# Tool base names (19 tools)
TOOL_BASES=(connect send poll ack claim complete cancel release lock unlock set_status rename status roster history end list cull cleanup retract)

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
    # Spoke: allowlist both trio (local) and quartet (to hub)
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
    echo "  /quartet: Start quartet_server.py to serve spokes"
    echo ""
    echo "  Server:   $NATIVE_PATH"
    echo "  Python:   $NATIVE_VENV_PY (dedicated venv)"
    echo "  Database: $DB_DIR/nth.db (created on first use)"
    echo ""
    echo "  To serve spoke /quartet sessions:"
    echo "    $VENV_PY $SERVER_DIR/quartet_server.py"
    echo "  (SSE on 0.0.0.0:8000 — accessible via Tailscale)"
else
    echo "  /quartet: nth-qweb (SSE -> $HUB_URL)"
    echo "  Python:   $NATIVE_VENV_PY (dedicated venv)"
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
echo ""
echo "Watch channel traffic live from a terminal (no Claude session needed):"
echo "  python3 $SERVER_DIR/nth_console.py              # follow all channels"
echo "  python3 $SERVER_DIR/nth_console.py -c MYCHAN    # one channel"
echo "  python3 $SERVER_DIR/nth_console.py --snapshot   # print + exit"
echo ""
echo "Dashboard view for 3-8 agent group chats (needs 'pip install rich'):"
echo "  python3 $SERVER_DIR/nth_dashboard.py MYCHAN     # per-agent engagement signals"
echo "  (Keys inside: s cycle sort · p pause · i type-a-message · q quit)"
echo ""
echo "Web dashboard for browser access over Tailscale (stdlib only):"
echo "  python3 $SERVER_DIR/nth_web.py MYCHAN           # loopback only (http://127.0.0.1:8765/)"
echo "  python3 $SERVER_DIR/nth_web.py MYCHAN --tailnet # reachable from tailnet peers"
echo ""
echo "  (Windows: substitute 'py' for 'python3')"
