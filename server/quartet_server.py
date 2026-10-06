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


def _install_auto_reinit_patch():
    # The MCP SDK's per-transport session table is in-memory only, so a hub
    # restart wipes it. Clients that hold a stale session_id and POST a
    # tools/call without re-sending `initialize` get rejected with -32602
    # "Invalid request parameters" / log "Received request before
    # initialization was complete" and stay stuck forever.
    #
    # That includes Claude Code's built-in MCP client today — there's no
    # client-side knob we can flip to make it re-handshake. So the hub
    # absorbs the inconvenience: on the first non-init request from an
    # uninitialized session, synthesize a default InitializeRequestParams
    # and flip the state to Initialized before passing the call through.
    # Behavior for properly-initialized sessions is unchanged.
    import mcp.server.session as _ss
    import mcp.types as _t

    # These are private SDK internals under a floating `mcp<2` pin. If a
    # point release renames one, skip the shim with a loud warning rather
    # than raising AttributeError at import — which would fail the unit and
    # leave systemd crash-looping the hub on a box nobody is sitting at.
    # Tested against mcp 1.29.0.
    if not hasattr(_ss, "ServerSession") or not hasattr(
            getattr(_ss, "ServerSession", None), "_received_request"):
        sys.stderr.write(
            "[quartet] WARNING: mcp SDK changed — auto-reinit shim skipped. "
            "Spokes may need to reconnect after a hub restart. "
            "Pin a known-good SDK if this persists.\n")
        sys.stderr.flush()
        return
    if not hasattr(_ss, "InitializationState"):
        sys.stderr.write(
            "[quartet] WARNING: mcp SDK changed (InitializationState missing) — "
            "auto-reinit shim skipped.\n")
        sys.stderr.flush()
        return

    _orig = _ss.ServerSession._received_request

    async def _patched(self, responder):
        req = responder.request.root
        not_init = self._initialization_state != _ss.InitializationState.Initialized
        if not_init and not isinstance(req, (_t.InitializeRequest, _t.PingRequest)):
            self._initialization_state = _ss.InitializationState.Initialized
            if self._client_params is None:
                self._client_params = _t.InitializeRequestParams(
                    protocolVersion=_t.LATEST_PROTOCOL_VERSION,
                    capabilities=_t.ClientCapabilities(),
                    clientInfo=_t.Implementation(
                        name="auto-reinit-shim", version="1.0"
                    ),
                )
            sys.stderr.write(
                f"[quartet] auto-reinit session for {type(req).__name__} "
                f"(client skipped initialize after reconnect)\n"
            )
            sys.stderr.flush()
        return await _orig(self, responder)

    _ss.ServerSession._received_request = _patched


_install_auto_reinit_patch()


def _install_thread_offload_patch():
    # FastMCP runs SYNC tool handlers directly on the asyncio event loop
    # (func_metadata.call_fn_with_arg_validation: `return fn(**args)`), so any
    # blocking handler — notably nth_poll's up-to-30s time.sleep long-poll —
    # freezes the ENTIRE server: every other session's request stalls behind it
    # (head-of-line blocking -> multi-second TTFB for everyone).
    #
    # Fix: offload sync handlers to anyio's worker-thread pool so the event loop
    # stays free and all handlers run in parallel. Each handler opens its own
    # sqlite connection (get_db) used within a single call, so threading is safe.
    import anyio
    import mcp.server.fastmcp.utilities.func_metadata as _fmod

    _orig = _fmod.FuncMetadata.call_fn_with_arg_validation
    _state = {"limiter_raised": False}

    async def _patched(self, fn, fn_is_async, arguments_to_validate,
                       arguments_to_pass_directly):
        if fn_is_async:
            return await _orig(self, fn, fn_is_async, arguments_to_validate,
                               arguments_to_pass_directly)

        async def _threaded(**kwargs):
            if not _state["limiter_raised"]:
                try:
                    anyio.to_thread.current_default_thread_limiter().total_tokens = 256
                except Exception:
                    pass
                _state["limiter_raised"] = True
            return await anyio.to_thread.run_sync(lambda: fn(**kwargs))

        # Reuse the original parsing/validation; only the fn() call moves to a thread.
        return await _orig(self, _threaded, True, arguments_to_validate,
                           arguments_to_pass_directly)

    _fmod.FuncMetadata.call_fn_with_arg_validation = _patched


_install_thread_offload_patch()

from nth_server import mcp


def _register_health_routes():
    """Plain-HTTP observability on the same uvicorn app that serves /sse.

    GET /healthz — cheap liveness: version, db_ok, counts. 503 if DB down.
    GET /fleet   — nodes + per-channel liveness. Counts, names, and ages
    only — never message content. No auth by design: the exposure surface
    (LAN + tailnet) and sensitivity match the SSE endpoint itself.
    """
    import anyio
    from datetime import datetime, timezone
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    import nth_server
    from nth_constants import NTH_VERSION

    STALE_S = 300  # matches STALE_THRESHOLD_SECONDS / monitor heartbeat model

    def _age_s(iso, now):
        if not iso:
            return None
        try:
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0, int((now - ts).total_seconds()))
        except ValueError:
            return None

    def _snapshot(deep):
        now = datetime.now(timezone.utc)
        out = {
            "server": "nth-qweb",
            "version": NTH_VERSION,
            "db_ok": False,
            "time": now.isoformat(),
        }
        try:
            db = nth_server.get_db()
        except Exception as e:
            out["error"] = type(e).__name__
            return out
        try:
            out["channels_active"] = db.execute(
                "SELECT COUNT(*) FROM channels WHERE status = 'active'"
            ).fetchone()[0]
            node_rows = db.execute(
                "SELECT hostname, transport, nth_version, python, pid, last_seen "
                "FROM nodes ORDER BY last_seen DESC"
            ).fetchall()
            nodes = []
            for r in node_rows:
                age = _age_s(r["last_seen"], now)
                nodes.append({
                    "hostname": r["hostname"], "transport": r["transport"],
                    "nth_version": r["nth_version"], "python": r["python"],
                    "pid": r["pid"], "last_seen": r["last_seen"],
                    "age_s": age,
                    "live": age is not None and age < STALE_S,
                })
            out["nodes_total"] = len(nodes)
            out["nodes_live"] = sum(1 for n in nodes if n["live"])
            out["db_ok"] = True
            if not deep:
                return out

            out["nodes"] = nodes
            channels = []
            for ch in db.execute(
                "SELECT code, status FROM channels ORDER BY code"
            ).fetchall():
                members = db.execute(
                    "SELECT messenger_heartbeat FROM members WHERE channel = ?",
                    (ch["code"],),
                ).fetchall()
                live = sum(
                    1 for m in members
                    if (a := _age_s(m["messenger_heartbeat"], now)) is not None
                    and a < STALE_S
                )
                msgs, last_msg = db.execute(
                    "SELECT COUNT(*), MAX(created_at) FROM messages WHERE channel = ?",
                    (ch["code"],),
                ).fetchone()
                channels.append({
                    "code": ch["code"], "status": ch["status"],
                    "members": len(members), "live": live, "msgs": msgs,
                    "last_msg_age_s": _age_s(last_msg, now),
                })
            channels.sort(
                key=lambda c: c["last_msg_age_s"] if c["last_msg_age_s"] is not None
                else float("inf")
            )
            out["channels"] = channels
            return out
        except Exception as e:
            out["error"] = type(e).__name__
            out["db_ok"] = False
            return out
        finally:
            db.close()

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        data = await anyio.to_thread.run_sync(lambda: _snapshot(False))
        return JSONResponse(data, status_code=200 if data.get("db_ok") else 503)

    @mcp.custom_route("/fleet", methods=["GET"])
    async def fleet(request: Request) -> JSONResponse:
        data = await anyio.to_thread.run_sync(lambda: _snapshot(True))
        return JSONResponse(data, status_code=200 if data.get("db_ok") else 503)

    # Check the hub itself in at startup so /fleet shows this process
    # before the first tool call arrives.
    try:
        _db = nth_server.get_db()
        nth_server._checkin_self_node(_db, force=True)
        _db.close()
    except Exception:
        pass


_register_health_routes()


def _run_dual_transport():
    """Serve BOTH MCP transports on one port.

    - /sse + /messages — legacy SSE (Claude Code registrations, nth_spoke_monitor)
    - /mcp            — streamable HTTP (modern clients: Codex's rmcp POSTs
                        here; POSTing to /sse gets 405, which is how this
                        gap was found)

    Custom routes (/healthz, /fleet) ride along on the SSE app's route table.
    The streamable app's lifespan must drive the composite: it starts the
    session manager task group; the SSE app's lifespan is a no-op default.
    """
    import uvicorn
    from starlette.applications import Starlette

    sse_app = mcp.sse_app()
    http_app = mcp.streamable_http_app()
    composite = Starlette(
        routes=[*http_app.routes, *sse_app.routes],
        lifespan=http_app.router.lifespan_context,
    )
    uvicorn.run(
        composite,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )


if __name__ == "__main__":
    _run_dual_transport()
