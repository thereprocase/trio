#!/usr/bin/env python3
"""nth doctor — one-glance health check for an nth install (hub or spoke).

Answers "wtf is up" from any machine, stdlib only, no venv needed:

    python3 nth_doctor.py            # one-shot table, exit 0 = all green
    python3 nth_doctor.py --watch    # same table, refreshed every 5s
    python3 nth_doctor.py --hub URL  # override hub base URL

Checks, in order:
  registration  nth-trio present in ~/.claude.json and its interpreter +
                server script exist on disk
  mcp import    the REGISTERED python can import FastMCP (catches OS
                python upgrades orphaning site-packages, and mcp 2.x)
  install       installed server files + their NTH_VERSION
  database      local DB opens read-only; channel/message counts
  hub           /healthz answers (URL from nth-qweb registration, else
                localhost:8000 on a hub box); reports hub version
  version       installed local version == hub version
  monitor       a monitor heartbeat row for this host is fresh (<5 min)
  fleet         node check-in table from hub /fleet (fallback: local DB)

Diagnosis only — never writes to the DB, never restarts anything.
"""
import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_JSON = Path.home() / ".claude.json"
DB_PATH = Path.home() / ".claude" / "nth" / "nth.db"
INSTALL_DIR = Path.home() / ".claude" / "skills" / "nth" / "server"
STALE_S = 300
HTTP_TIMEOUT = 4

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: ("\033[32m", "+"), WARN: ("\033[33m", "!"), FAIL: ("\033[31m", "x")}


def _color_enabled():
    return sys.stdout.isatty() or os.environ.get("FORCE_COLOR")


def _age_str(seconds):
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _iso_age_s(iso):
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except ValueError:
        return None


def _read_registration():
    """Return (stdio_cfg, hub_url) from ~/.claude.json, either may be None."""
    try:
        cfg = json.loads(CLAUDE_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    servers = cfg.get("mcpServers", {})
    stdio = servers.get("nth-trio")
    hub_url = None
    qweb = servers.get("nth-qweb", {})
    if isinstance(qweb, dict) and qweb.get("url"):
        # Strip the /sse suffix to get the HTTP base the health routes live on.
        hub_url = re.sub(r"/sse/?$", "", qweb["url"])
    return stdio, hub_url


def _installed_version():
    """Parse NTH_VERSION out of the installed constants file (no import —
    avoids executing install-dir code just to read one string)."""
    try:
        text = (INSTALL_DIR / "nth_constants.py").read_text()
    except OSError:
        return None
    m = re.search(r'^NTH_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else None


def _http_json(url):
    """GET url, parse JSON. Returns (data, latency_ms, error_str)."""
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return data, int((time.monotonic() - t0) * 1000), None
    except urllib.error.HTTPError as e:
        # /healthz deliberately 503s with a JSON body when the DB is down —
        # that body is still worth showing.
        try:
            return json.loads(e.read().decode()), None, f"HTTP {e.code}"
        except Exception:
            return None, None, f"HTTP {e.code}"
    except Exception as e:
        return None, None, type(e).__name__


def run_checks(hub_override=None):
    """Return (checks, fleet_rows). checks = [(label, level, detail)]."""
    checks = []
    stdio, hub_url = _read_registration()
    if hub_override:
        hub_url = hub_override.rstrip("/")

    # --- registration ---
    reg_python = None
    if not stdio:
        checks.append(("registration", FAIL,
                       "nth-trio missing from ~/.claude.json — run setup.sh"))
    else:
        reg_python = stdio.get("command", "")
        script = (stdio.get("args") or [""])[0]
        missing = [p for p in (reg_python, script) if p and not Path(p).exists()]
        if missing:
            checks.append(("registration", FAIL,
                           f"registered path missing: {missing[0]}"))
            reg_python = None
        else:
            venv_tag = " (venv)" if "/nth/venv/" in reg_python or "\\nth\\venv\\" in reg_python else " (SYSTEM python — rerun setup.sh)"
            checks.append(("registration", OK, f"nth-trio -> {reg_python}{venv_tag}"))

    # --- mcp import with the registered interpreter ---
    if reg_python:
        try:
            proc = subprocess.run(
                [reg_python, "-c",
                 "from mcp.server.fastmcp import FastMCP;"
                 "import importlib.metadata as im; print(im.version('mcp'))"],
                capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                checks.append(("mcp import", OK,
                               f"FastMCP ok (mcp {proc.stdout.strip()})"))
            else:
                err = (proc.stderr.strip().splitlines() or ["import failed"])[-1]
                checks.append(("mcp import", FAIL, err[:100]))
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append(("mcp import", FAIL, type(e).__name__))
    else:
        checks.append(("mcp import", FAIL, "no usable registration to test"))

    # --- installed files + version ---
    local_version = _installed_version()
    if not (INSTALL_DIR / "nth_server.py").exists():
        checks.append(("install", FAIL, f"{INSTALL_DIR} missing — run setup.sh"))
    elif not local_version:
        checks.append(("install", WARN,
                       "server files present but no NTH_VERSION (pre-7.3 install)"))
    else:
        checks.append(("install", OK, f"v{local_version} at {INSTALL_DIR}"))

    # --- local database ---
    db = None
    if DB_PATH.exists():
        try:
            db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            nch, = db.execute("SELECT COUNT(*) FROM channels").fetchone()
            nmsg, = db.execute("SELECT COUNT(*) FROM messages").fetchone()
            checks.append(("database", OK,
                           f"{DB_PATH.name}: {nch} channels, {nmsg} msgs"))
        except sqlite3.Error as e:
            checks.append(("database", FAIL, f"{type(e).__name__}: {e}"))
            db = None
    else:
        checks.append(("database", WARN, "no local DB yet (created on first use)"))

    # --- hub /healthz ---
    hub_version = None
    hub_fleet = None
    if not hub_url:
        # A hub box has no nth-qweb registration; its own quartet server
        # answers on localhost.
        hub_url = "http://127.0.0.1:8000"
        hub_label = "localhost:8000 (no nth-qweb registration; assuming hub box)"
    else:
        hub_label = hub_url
    health, ms, err = _http_json(f"{hub_url}/healthz")
    if health and health.get("db_ok"):
        hub_version = health.get("version")
        checks.append(("hub", OK,
                       f"{hub_label} v{hub_version} "
                       f"({health.get('channels_active', '?')} active ch, {ms}ms)"))
        hub_fleet, _, _ = _http_json(f"{hub_url}/fleet")
    elif health:
        checks.append(("hub", FAIL,
                       f"{hub_label} answered but DB down ({err or 'db_ok false'})"))
    else:
        checks.append(("hub", FAIL, f"{hub_label} unreachable ({err})"))

    # --- version match ---
    if local_version and hub_version:
        if local_version == hub_version:
            checks.append(("version", OK, f"local {local_version} == hub {hub_version}"))
        else:
            checks.append(("version", WARN,
                           f"drift: local {local_version} vs hub {hub_version}"))
    else:
        checks.append(("version", WARN, "cannot compare (missing local or hub version)"))

    # --- monitor on this host ---
    host = socket.gethostname()
    mon_detail, mon_level = "no check-in row (no monitor has run here)", WARN
    if db:
        try:
            row = db.execute(
                "SELECT last_seen FROM nodes WHERE hostname = ? AND transport = 'monitor'",
                (host,)).fetchone()
            if row:
                age = _iso_age_s(row["last_seen"])
                if age is not None and age < STALE_S:
                    mon_level, mon_detail = OK, f"heartbeat {_age_str(age)} ago"
                else:
                    mon_detail = f"last heartbeat {_age_str(age)} ago (not running)"
        except sqlite3.OperationalError:
            mon_detail = "nodes table absent (pre-7.3 DB)"
    checks.append(("monitor", mon_level, mon_detail))

    # --- fleet rows (hub view preferred, local fallback) ---
    fleet_rows = []
    if hub_fleet and hub_fleet.get("nodes") is not None:
        fleet_rows = [(n["hostname"], n["transport"], n.get("nth_version") or "?",
                       n.get("age_s"), n.get("live"))
                      for n in hub_fleet["nodes"]]
    elif db:
        try:
            for r in db.execute(
                    "SELECT hostname, transport, nth_version, last_seen "
                    "FROM nodes ORDER BY last_seen DESC"):
                age = _iso_age_s(r["last_seen"])
                fleet_rows.append((r["hostname"], r["transport"],
                                   r["nth_version"] or "?", age,
                                   age is not None and age < STALE_S))
        except sqlite3.OperationalError:
            pass
    if db:
        db.close()
    return checks, fleet_rows


def render(checks, fleet_rows, color=True):
    lines = []
    worst = OK
    host = socket.gethostname()
    lines.append(f"nth doctor — {host} — {datetime.now().strftime('%H:%M:%S')}")
    lines.append("-" * 64)
    for label, level, detail in checks:
        c, mark = _MARK[level]
        if not color:
            c, reset = "", ""
        else:
            reset = "\033[0m"
        lines.append(f" {c}{mark}{reset} {label:<12s} {detail}")
        if level == FAIL:
            worst = FAIL
        elif level == WARN and worst != FAIL:
            worst = WARN
    if fleet_rows:
        lines.append("")
        lines.append(" fleet (node check-ins):")
        for hostname, transport, ver, age, live in fleet_rows:
            state = "live " if live else "stale"
            if color:
                sc = "\033[32m" if live else "\033[90m"
                state = f"{sc}{state}\033[0m"
            lines.append(f"   {hostname:<14s} {transport:<8s} v{ver:<8s} "
                         f"{state} {_age_str(age)}")
    return "\n".join(lines), worst


def main():
    ap = argparse.ArgumentParser(description="nth health check")
    ap.add_argument("--watch", action="store_true", help="refresh every 5s")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--hub", help="hub base URL override (e.g. http://pve:8000)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    color = not args.no_color and _color_enabled()

    if args.watch:
        try:
            while True:
                text, _ = render(*run_checks(args.hub), color=color)
                # Clear + home, then repaint. Plain ANSI, no deps.
                sys.stdout.write("\033[2J\033[H" + text +
                                 f"\n\n (watch: {args.interval:g}s — Ctrl-C to exit)\n")
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    text, worst = render(*run_checks(args.hub), color=color)
    print(text)
    return 1 if worst == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
