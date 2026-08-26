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
    for f in nth_server.py nth_monitor.py nth_spoke_monitor.py nth_console.py \
             nth_dashboard.py nth_web.py nth_stt_worker.py quartet_server.py \
             nth_constants.py nth_doctor.py codex_context_publisher.py \
             nth_supervisor.py nth_request_log.py \
             nth_agent_manager.py \
             nth_codex_runtime.py \
             nth_usage.py nth_conversation.py nth_ask_client.js; do
        if [ -f "$HUB_DIR/$f" ] && ! cmp -s "$SCRIPT_DIR/server/$f" "$HUB_DIR/$f"; then
            cp "$HUB_DIR/$f" "$HUB_DIR/$f.bak-$STAMP"
        fi
        cp "$SCRIPT_DIR/server/$f" "$HUB_DIR/$f"
    done

    # The browser bundle. nth_web.py composes its page from these files at
    # IMPORT time, so without them the dashboard does not start at all — it
    # raises before it can serve or log a thing. Copied as a DIRECTORY rather
    # than as named files so that adding a CSS layer never requires an edit
    # here; a named list is exactly what drifted for the Python modules above.
    rm -rf "${HUB_DIR:?}/web"
    cp -R "$SCRIPT_DIR/server/web" "$HUB_DIR/web"

    # Dedicated venv — same rationale and pin as spoke mode (mcp 2.0 removed
    # FastMCP; OS python upgrades orphan site-packages). Wheels only.
    HUB_VENV="$HUB_DIR/venv"
    if ! "$HUB_VENV/bin/python" -c "import sys" &>/dev/null; then
        echo "Creating hub venv: $HUB_VENV"
        rm -rf "$HUB_VENV"
        python3 -m venv "$HUB_VENV"
    fi
    if ! "$HUB_VENV/bin/python" -c "from mcp.server.fastmcp import FastMCP; import uvicorn" &>/dev/null; then
        echo "Installing into hub venv: mcp<2 uvicorn==0.52.1"
        "$HUB_VENV/bin/python" -m pip install --quiet --upgrade pip
        "$HUB_VENV/bin/python" -m pip install --quiet --only-binary :all: "mcp<2" "uvicorn==0.52.1"
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
# HOME redirection keeps the shared DB out of /root: everything lives under
# ${HUB_HOME}/.claude/nth/ and survives reinstalls via StateDirectory.
# NOTE: this relocates paths, it does NOT drop privileges — this unit still
# runs as root. See TODO.md "De-root the hub services" for the User= +
# chown migration; it is deliberately not automated because re-owning a
# live ${HUB_HOME} mid-upgrade would lock the hub out of its own database.
Environment=HOME=${HUB_HOME}
StateDirectory=quartet-hub
WorkingDirectory=${HUB_DIR}
ExecStart=${HUB_VENV}/bin/python ${HUB_DIR}/quartet_server.py
Restart=on-failure
RestartSec=3
# Blast-radius reduction for a network-facing, no-auth-by-design service.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

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
ExecStart=${HUB_VENV}/bin/python ${HUB_DIR}/nth_web.py --tailscale-tls --port 8765
Restart=on-failure
RestartSec=3
# See quartet-hub.service: still root, hardened. --tailscale-tls binds 0.0.0.0
# with no authentication — the host firewall / Tailscale ACL is the gate.
# It serves https rather than plain http because browsers grant microphone
# access only on a secure context: under --tailnet this unit shipped a hub
# whose dictation could never work from any device, and said so only in a
# service log. Needs HTTPS Certificates enabled for the tailnet
# (https://login.tailscale.com/admin/dns); the server falls back to an
# existing certificate if a renewal fails, and refuses to start only when it
# has none — deliberately, since silently serving http would restore the bug.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

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

# hub/spoke install into $HOME. Under sudo that is /root, so everything —
# skills, venv, MCP registration — lands where the user's own Claude Code
# will never look, while setup still prints "Setup Complete". Only
# hub-service (handled above, and already exited) legitimately needs root.
if [ "$(id -u)" = "0" ] && [ -n "${SUDO_USER:-}" ]; then
    echo "ERROR: don't run hub/spoke setup with sudo — it would install into"
    echo "       /root instead of /home/${SUDO_USER}."
    echo "       Run it as yourself:  bash setup.sh ${1:-hub}"
    echo "       (Only 'sudo bash setup.sh hub-service' needs root.)"
    exit 1
fi

case "${1:-}" in
    hub)          MODE="hub";   shift ;;
    spoke|remote) MODE="spoke"; shift ;;
esac

if [ -z "$MODE" ] && [ ! -t 0 ]; then
    echo "ERROR: no mode given and stdin isn't a terminal."
    echo "       Run: bash setup.sh hub   |   bash setup.sh spoke <hub-sse-url>"
    exit 1
fi

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
    # Catch the three mistakes that otherwise register cleanly and only
    # fail much later as an opaque MCP error after a Claude Code restart.
    case "$HUB_URL" in
        http://*|https://*) ;;
        *)
            echo "ERROR: hub URL needs a scheme, e.g. http://${HUB_URL}"
            exit 1
            ;;
    esac
    case "$HUB_URL" in
        *:8765*)
            echo "ERROR: :8765 is the web dashboard, not the SSE endpoint."
            echo "       Spokes want the hub's MCP port, usually :8000/sse"
            exit 1
            ;;
    esac
    case "$HUB_URL" in
        */sse|*/sse/) ;;
        *)
            echo "WARNING: hub URL usually ends in /sse — got: $HUB_URL"
            ;;
    esac
    # Probe before declaring success. /healthz lives at the server root.
    HUB_BASE="${HUB_URL%/}"; HUB_BASE="${HUB_BASE%/sse}"
    if command -v curl &>/dev/null; then
        if curl -fsS -m 5 "${HUB_BASE}/healthz" >/dev/null 2>&1; then
            echo "Hub reachable: ${HUB_BASE}/healthz OK"
        else
            echo "WARNING: no answer from ${HUB_BASE}/healthz"
            echo "         Registering anyway — check the hub is running and"
            echo "         that Tailscale is up on both machines, then run:"
            echo "         nth-doctor"
        fi
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
    VENV_PKGS+=("uvicorn==0.52.1")
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
# The optional dictation sidecar. nth_web.py resolves it as a sibling of itself,
# so omitting it here leaves the mic button present but the engine missing.
cp "$SCRIPT_DIR/server/nth_stt_worker.py" "$SERVER_DIR/nth_stt_worker.py"
cp "$SCRIPT_DIR/server/quartet_server.py" "$SERVER_DIR/quartet_server.py"
cp "$SCRIPT_DIR/server/nth_constants.py" "$SERVER_DIR/nth_constants.py"
# The agent supervisor and its per-request token log. nth_web imports both at
# module scope, so omitting them stops the dashboard importing at all — and
# only on an installed copy, never from the repo, where every sibling is
# present. tests/test-install-manifest.py enforces this against BOTH lists.
cp "$SCRIPT_DIR/server/nth_supervisor.py" "$SERVER_DIR/nth_supervisor.py"
cp "$SCRIPT_DIR/server/nth_request_log.py" "$SERVER_DIR/nth_request_log.py"
cp "$SCRIPT_DIR/server/nth_codex_runtime.py" "$SERVER_DIR/nth_codex_runtime.py"
cp "$SCRIPT_DIR/server/nth_agent_manager.py" "$SERVER_DIR/nth_agent_manager.py"
# Quota-burn series + the arithmetic over it.
cp "$SCRIPT_DIR/server/nth_usage.py" "$SERVER_DIR/nth_usage.py"
# Conversation identity (canonical DM thread keys). nth_web imports it at
# module scope.
cp "$SCRIPT_DIR/server/nth_conversation.py" "$SERVER_DIR/nth_conversation.py"
# The ask-picker helpers. Not a Python import but read at IMPORT time all the
# same — nth_web inlines this file into the page — so an install missing it
# raises before the dashboard can serve anything, exactly like a missing
# module. It lives outside server/web/ so Node can require() it in tests.
cp "$SCRIPT_DIR/server/nth_ask_client.js" "$SERVER_DIR/nth_ask_client.js"
cp "$SCRIPT_DIR/server/nth_doctor.py" "$SERVER_DIR/nth_doctor.py"
cp "$SCRIPT_DIR/server/nth_spoke_monitor.py" "$SERVER_DIR/nth_spoke_monitor.py"
cp "$SCRIPT_DIR/server/codex_context_publisher.py" "$SERVER_DIR/codex_context_publisher.py"
# The browser bundle — index.html plus the ordered CSS/JS layers. nth_web.py
# reads these at IMPORT time to compose the served page, so an install without
# them cannot start the dashboard at all. Copied as a DIRECTORY so a new asset
# never needs an edit here. See tests/test-install-manifest.py.
rm -rf "${SERVER_DIR:?}/web"
cp -R "$SCRIPT_DIR/server/web" "$SERVER_DIR/web"

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
cp "$SCRIPT_DIR/server/nth_turn_hook.py" "$SERVER_DIR/nth_turn_hook.py"
cp "$SCRIPT_DIR/server/nth_activity_hook.py" "$SERVER_DIR/nth_activity_hook.py"
cp "$SCRIPT_DIR/server/nth_stall_hook.py" "$SERVER_DIR/nth_stall_hook.py"

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

# Tool base names — must match the @mcp.tool registrations in nth_server.py
# (21 tools). Verify after adding a tool:
#   diff <(rg -o 'def nth_(\w+)' server/nth_server.py | sed 's/def nth_//' | sort) \
#        <(printf '%s\n' "${TOOL_BASES[@]}" | sort)
# `pounds` was missing here through v8.0.1 while SKILL told `at`-mode agents
# to call it on every wake, so the one routinely-called tool was the one that
# always prompted.
TOOL_BASES=(connect send poll ack claim complete cancel release lock unlock set_status rename status roster history end list cull cleanup retract pounds)

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

# Triple-quoted so an apostrophe in the path (e.g. /Users/O'Brien) can't abort
# the install with an unterminated string literal.
settings_path = r'''$SETTINGS_JSON'''
tools = $(printf '%s\n' "${ALL_TOOLS[@]}" | "$PYTHON_CMD" -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))")
old_patterns = '$OLD_PATTERNS'.split()

if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except ValueError as e:
        # Bail with instructions rather than a traceback: MCP registration
        # has already happened by this point, so the user is mid-install.
        print('')
        print('WARNING: ' + settings_path + ' is not valid JSON (' + str(e) + ').')
        print('Tools were NOT allowlisted. Fix that file and re-run setup,')
        print('or add these under permissions.allow yourself:')
        for t in tools:
            print('  ' + t)
        raise SystemExit(0)
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

# Atomic replace + one backup: a crash or full disk mid-write would
# otherwise truncate the user's global Claude settings.
if os.path.exists(settings_path):
    try:
        with open(settings_path) as src, open(settings_path + '.bak', 'w') as dst:
            dst.write(src.read())
    except OSError:
        pass
tmp_path = settings_path + '.tmp'
with open(tmp_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')
os.replace(tmp_path, settings_path)

print(f'Permissions: {added} tool(s) allowlisted, {len(removed)} old entries removed')
"

# ---------- 7b. Register the working-indicator hooks ----------
# Two hooks:
#   nth_turn_hook     (Stop + StopFailure)       -> stamps last_turn_end so the
#                                                   dashboard shows working vs. idle.
#   nth_activity_hook (PreToolUse + UserPromptSubmit) -> stamps last_seen on every
#                                                   tool/prompt so 'working' spans
#                                                   the whole turn, not just from
#                                                   the first trio call.
# Idempotent per (event, script) — re-running setup.sh never duplicates.
native_path() {  # native_path <posix-path>
    if [ "$PLATFORM" = "windows" ]; then
        if command -v cygpath &>/dev/null; then cygpath -w "$1"
        else echo "$1" | sed 's|^/\([a-zA-Z]\)/|\1:\\|' | sed 's|/|\\|g'; fi
    else
        echo "$1"
    fi
}
TURN_NATIVE=$(native_path "$SERVER_DIR/nth_turn_hook.py")
ACTIVITY_NATIVE=$(native_path "$SERVER_DIR/nth_activity_hook.py")
STALL_NATIVE=$(native_path "$SERVER_DIR/nth_stall_hook.py")

"$PYTHON_CMD" -c "
import json, os, tempfile
# Triple-quoted raw strings so an apostrophe in the path (e.g. /Users/O'Brien)
# doesn't produce an unterminated string literal and abort the install.
settings_path = r'''$SETTINGS_JSON'''
py = r'''$PYTHON_CMD'''
turn  = r'''$TURN_NATIVE'''
activity = r'''$ACTIVITY_NATIVE'''
stall = r'''$STALL_NATIVE'''
turn_cmd  = f'{py} \"{turn}\"'
activity_cmd = f'{py} \"{activity}\"'
stall_cmd = f'{py} \"{stall}\"'

# Match every StopFailure error type (not just the transient ones): the watchdog
settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (ValueError, OSError) as e:
        # Don't abort the whole install on a malformed/unreadable settings.json —
        # skip the hooks and tell the user to add them by hand (see CHANGELOG).
        print(f'trio hooks: SKIPPED (could not read {settings_path}: {e})')
        raise SystemExit(0)
if not isinstance(settings, dict):
    settings = {}

hooks = settings.setdefault('hooks', {})
if not isinstance(hooks, dict):
    print('trio hooks: SKIPPED (settings.hooks is not an object)')
    raise SystemExit(0)

def register(event, marker, cmd):
    arr = hooks.setdefault(event, [])
    if not isinstance(arr, list):
        print(f'trio hooks: SKIPPED ({event} is not a list)')
        return False
    if any(marker in json.dumps(e) for e in arr):
        return False  # already present
    arr.append({'hooks': [{'type': 'command', 'command': cmd}]})
    return True

changed = False
# The turn hook must fire on EVERY turn end, including StopFailure error types
# we have never seen — a scoped matcher would let a session that acted mid-turn
# show a false 'working' forever — so both events are registered unscoped.
changed |= register('Stop',        'nth_turn_hook.py',  turn_cmd)
changed |= register('StopFailure', 'nth_turn_hook.py',  turn_cmd)
# The activity hook stamps sessions.last_seen on every tool call and prompt so
# the dashboard shows 'working' for the whole active turn (not just from the
# agent's first trio call). Matcher-less on both events — every tool/prompt is
# activity, regardless of kind.
changed |= register('PreToolUse',       'nth_activity_hook.py', activity_cmd)
changed |= register('UserPromptSubmit', 'nth_activity_hook.py', activity_cmd)
# PostToolUse too: it is what clears blocked_since when the human answers an
# AskUserQuestion/ExitPlanMode. Without it a session stays flagged blocked until
# the turn ends, which is most of the window the flag is supposed to cover.
changed |= register('PostToolUse',      'nth_activity_hook.py', activity_cmd)
# Records a turn that died to an API error, so a frozen session shows STALLED
# on the roster instead of looking healthy. Matcher-less on every StopFailure
# error type: a matcher would drop an unrecognised error before anyone saw it,
# and the badge's whole job is to make the unnoticed visible.
changed |= register('StopFailure', 'nth_stall_hook.py', stall_cmd)

if not changed:
    print('trio hooks: already registered')
else:
    # Atomic write: a crash/disk-full mid-write must never truncate the user's
    # settings.json and lose unrelated settings.
    d = os.path.dirname(settings_path) or '.'
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.settings-', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(settings, f, indent=2)
            f.write('\n')
        os.replace(tmp, settings_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print('trio hooks: registered (working indicator)')
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
    echo ""
    echo "  Spoke sessions get event-driven wakes (no polling): after"
    echo "  quartet_connect, launch the command from its monitor_hint field —"
    echo "  nth_spoke_monitor.py speaking MCP-over-SSE to the hub:"
    echo "    python3 $SERVER_DIR/nth_spoke_monitor.py CHAN MEMBER_ID --filter about \\"
    echo "      --url $HUB_URL"
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
echo "  python3 $SERVER_DIR/nth_web.py MYCHAN --tailscale-tls  # https — REQUIRED for dictation"
echo "     (browsers only grant microphone access on https or localhost, so over"
echo "      plain http at a tailnet IP dictation cannot work from any device."
echo "      Needs HTTPS Certificates enabled: https://login.tailscale.com/admin/dns)"
echo ""
echo "  (Windows: substitute 'py' for 'python3')"
