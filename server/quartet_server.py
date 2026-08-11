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

if __name__ == "__main__":
    mcp.run(transport="sse")
