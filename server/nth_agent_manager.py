#!/usr/bin/env python3
"""Provider-neutral managed-agent lifecycle for the unified Trio hub."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import nth_supervisor as nsup

# Codex is an OPTIONAL provider. The dispatcher's job is to make the runtime a
# detail, and that has to include the runtime not being installed: a hub that
# only ever runs Claude should not import a 1200-line App Server transport, and
# a maintainer who does not use Codex should be able to drop the module without
# touching this file. Every self.codex use below is guarded accordingly.
try:
    from nth_codex_runtime import CodexRuntimeManager
except ImportError:                                        # pragma: no cover
    CodexRuntimeManager = None


# Re-exported, not redefined. This file used to carry its own copy of the
# catalogue, and because the dispatcher is what /api/agent-models actually
# calls, ITS copy was the one the picker showed — so editing the supervisor's
# list changed nothing visible and the two drifted silently. One definition,
# in nth_supervisor, which is where the Claude runtime lives.
#
# Names are bare model tiers (no "Claude " prefix) — the provider column
# already says Claude. `id` is the CLI alias passed straight to `claude
# --model`.
CLAUDE_MODELS = nsup.CLAUDE_MODELS


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UnifiedAgentSupervisor:
    """Dispatch lifecycle calls to Claude or Codex by durable agent provider."""

    def __init__(self, db_path: Path, *, nth_server_path: str = "",
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 claude: Optional[nsup.AgentSupervisor] = None,
                 codex: Optional[CodexRuntimeManager] = None):
        self.db_path = Path(db_path)
        # agent_id -> runtime_provider. Dict writes are atomic under the GIL
        # and the value is durable, so no lock is needed for this access
        # pattern (many readers, invalidation only from spawn/delete).
        self._provider_cache: Dict[str, str] = {}
        self.claude = claude or nsup.AgentSupervisor(
            db_path=self.db_path, on_event=on_event)
        self.codex = codex
        if self.codex is None and CodexRuntimeManager is not None:
            self.codex = CodexRuntimeManager(
                self.db_path, nth_server_path=nth_server_path, on_event=on_event)

    # Backward-compatible accessors used by Phase 4 diagnostics and tests.
    @property
    def runtime(self):
        return self.claude.runtime

    @property
    def _procs(self):
        return self.claude._procs

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def provider_for(self, agent_id: str) -> str:
        # Cached. This is called by manager_for, i.e. by EVERY dispatch —
        # is_running, wake, feed, hibernate, stop, interrupt, compact,
        # queued_count, is_busy, activity. Uncached it opened a fresh sqlite
        # connection each time (~0.6ms, almost all of it connection setup), and
        # the router calls is_running once per routed message per target agent,
        # so every message to any managed agent — Claude included — paid a DB
        # round trip this layer added.
        #
        # runtime_provider is durable and only spawn() changes it, which
        # invalidates below. A miss is a normal read, so a cold cache is
        # correct, just slower.
        cached = self._provider_cache.get(agent_id)
        if cached is not None:
            return cached
        db = self._db()
        try:
            row = db.execute(
                "SELECT runtime_provider FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
        finally:
            db.close()
        if row is None:
            # Not cached: an agent row can appear at any moment, and caching
            # "" would make a newly created agent permanently undispatchable.
            return ""
        provider = (row["runtime_provider"] or "claude").lower()
        self._provider_cache[agent_id] = provider
        return provider

    def forget_provider(self, agent_id: str) -> None:
        """Drop a cached provider — call whenever runtime_provider changes."""
        self._provider_cache.pop(agent_id, None)

    def providers(self) -> tuple:
        """Provider names this hub can actually dispatch to.

        Callers ask the dispatcher rather than hardcoding ("claude", "codex"):
        a hardcoded tuple in a handler means adding a third provider requires
        editing the web layer, which is exactly what this seam exists to
        prevent — and it would also advertise Codex on a hub where the module
        is not installed."""
        return ("claude", "codex") if self.codex is not None else ("claude",)

    def manager_for(self, agent_id: str):
        provider = self.provider_for(agent_id)
        if provider == "codex":
            return self.codex
        if provider == "claude":
            return self.claude
        return None

    def spawn(self, agent_id: str, *, provider: str = "", **kwargs):
        provider = (provider or self.provider_for(agent_id) or "claude").lower()
        if provider not in self.providers():
            raise ValueError(f"unsupported runtime provider: {provider}")
        db = self._db()
        try:
            db.execute("UPDATE agents SET runtime_provider=? WHERE id=?",
                       (provider, agent_id))
            db.commit()
        finally:
            db.close()
        # This is the only writer of runtime_provider, so it is the only place
        # the cache can go stale.
        self._provider_cache[agent_id] = provider
        manager = self.codex if provider == "codex" else self.claude
        return manager.spawn(agent_id, **kwargs)

    def wake(self, agent_id: str, **kwargs):
        manager = self.manager_for(agent_id)
        return manager.wake(agent_id, **kwargs) if manager else None

    def feed(self, agent_id: str, channel: str, text: str,
             attachments: Optional[List[str]] = None,
             source_message_id: int = 0, source_sender: str = "") -> bool:
        manager = self.manager_for(agent_id)
        if manager is self.codex:
            return self.codex.feed(
                agent_id, channel, text, attachments=attachments or [],
                source_message_id=source_message_id, source_sender=source_sender)
        return bool(manager and manager.feed(
            agent_id, channel, text, attachments=attachments or [],
            source_message_id=source_message_id, source_sender=source_sender))

    def hibernate(self, agent_id: str) -> bool:
        manager = self.manager_for(agent_id)
        return bool(manager and manager.hibernate(agent_id))

    def stop(self, agent_id: str) -> bool:
        manager = self.manager_for(agent_id)
        return bool(manager and manager.stop(agent_id))

    def interrupt(self, agent_id: str) -> bool:
        # BOTH providers implement interrupt; dispatch like everything else.
        #
        # An earlier version routed Claude to stop(), reasoning that "Claude's
        # stream protocol has no provider-level turn interrupt". That is
        # contradicted by AgentSupervisor.interrupt, and the difference is the
        # entire point of the method: it ends the turn but KEEPS session_id and
        # leaves the agent ST_SLEEPING, so a wake resumes the same transcript
        # with --resume. stop() lands it in ST_STOPPED and loses that. Routing
        # to stop silently changed behaviour for every existing Claude agent,
        # with no call-site diff to reveal it.
        manager = self.manager_for(agent_id)
        return bool(manager and manager.interrupt(agent_id))

    def compact(self, agent_id: str, message: str = "") -> bool:
        manager = self.manager_for(agent_id)
        return bool(manager and manager.compact(agent_id, message=message))

    def clear(self, agent_id: str, **kwargs):
        manager = self.manager_for(agent_id)
        if manager is None:
            return None
        self._record_runtime(agent_id, "cleared")
        return manager.clear(agent_id, **kwargs)

    def delete(self, agent_id: str) -> bool:
        manager = self.manager_for(agent_id)
        if manager is None:
            return False
        ok = self.codex.delete(agent_id) if manager is self.codex else manager.stop(agent_id)
        if ok:
            self._record_runtime(agent_id, "deleted")
            self.forget_provider(agent_id)
        return bool(ok)

    def _record_runtime(self, agent_id: str, disposition: str) -> None:
        db = self._db()
        try:
            row = db.execute(
                "SELECT runtime_provider, runtime_ref, session_id FROM agents WHERE id=?",
                (agent_id,)).fetchone()
            if row is not None:
                runtime_ref = row["runtime_ref"] or row["session_id"] or ""
                if runtime_ref:
                    db.execute(
                        "INSERT INTO agent_runtime_history "
                        "(agent_id,provider,runtime_ref,disposition,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (agent_id, row["runtime_provider"] or "claude", runtime_ref,
                         disposition, now_iso()))
                    db.commit()
        finally:
            db.close()

    def is_running(self, agent_id: str) -> bool:
        manager = self.manager_for(agent_id)
        return bool(manager and manager.is_running(agent_id))

    # ── identity/ownership guards ───────────────────────────────────────────
    #
    # This facade is hand-maintained, so a method added to AgentSupervisor is
    # not reachable through it until someone writes the forwarder — and the
    # failure is an AttributeError at the call site, not a signature mismatch
    # anything checks. Four of the five below were added by the B1 fix and
    # never forwarded, which is why test-agent-bulk, test-web-agents and
    # test-web-codex-agents fail with "no attribute 'reserve_starting'".
    #
    # These are the guards that keep one agent id naming one process. Losing
    # them to an AttributeError does not merely error: AgentRouter's worker
    # catches Exception per message, so a missing foreign_owner_pid would drop
    # every routed message while looking like a quiet hub.

    def _guard(self, agent_id: str, name: str):
        """The manager that implements `name` for this agent.

        Falls back to the Claude supervisor rather than to nothing: these
        guards read the shared `agents` table, so its implementation is
        correct for any provider, and a silently absent guard is the failure
        mode this whole block exists to prevent. A provider that wants its own
        answer supplies the method and is preferred.
        """
        manager = self.manager_for(agent_id)
        if manager is not None and hasattr(manager, name):
            return manager
        return self.claude

    def plock(self, agent_id: str):
        return self._guard(agent_id, "plock").plock(agent_id)

    def reserve_starting(self, agent_id: str) -> None:
        self._guard(agent_id, "reserve_starting").reserve_starting(agent_id)

    def release_starting(self, agent_id: str) -> None:
        self._guard(agent_id, "release_starting").release_starting(agent_id)

    def is_running_or_starting(self, agent_id: str) -> bool:
        return bool(self._guard(agent_id, "is_running_or_starting")
                    .is_running_or_starting(agent_id))

    def foreign_owner_pid(self, agent_id: str) -> Optional[int]:
        return self._guard(agent_id, "foreign_owner_pid").foreign_owner_pid(agent_id)

    def live_ids(self) -> List[str]:
        ids = set(self.claude.live_ids())
        if self.codex is not None:
            ids |= set(self.codex.live_ids())
        return sorted(ids)

    def queued_count(self, agent_id: str) -> int:
        manager = self.manager_for(agent_id)
        if manager is self.codex:
            return self.codex.queued_count(agent_id)
        return 0

    def is_busy(self, agent_id: str) -> bool:
        manager = self.manager_for(agent_id)
        return bool(manager and manager.is_busy(agent_id))

    def activity(self, agent_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        manager = self.manager_for(agent_id)
        if manager is self.codex:
            return self.codex.activity(agent_id, limit=limit)
        return []

    def pending_approvals(self) -> List[Dict[str, Any]]:
        out = list(self.claude.pending_approvals())
        if self.codex is not None:
            out += self.codex.pending_approvals()
        return out

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        # Claude's DB-backed approvals are tagged with a "cap_" id prefix
        # (see nth_supervisor.AgentSupervisor.pending_approvals) so a single
        # dashboard resolve endpoint can route to the right provider without
        # the caller needing to know which one raised it. Claude only
        # understands accept/decline (no Codex-style "acceptForSession").
        if approval_id.startswith("cap_") or self.codex is None:
            mapped = "accept" if decision in ("accept", "acceptForSession") else "decline"
            return self.claude.resolve_approval(approval_id, mapped)
        return self.codex.resolve_approval(approval_id, decision)

    def diagnostics(self, provider: str, *, deep: bool = False) -> Dict[str, Any]:
        provider = provider.lower()
        if provider == "claude":
            return self.claude.runtime.diagnostics()
        if provider == "codex" and self.codex is not None:
            return self.codex.diagnostics(deep=deep)
        return {"provider": provider, "ready": False,
                "detail": f"unsupported runtime provider: {provider}"}

    def list_models(self, provider: str) -> List[Dict[str, Any]]:
        if provider.lower() == "claude":
            return [dict(model) for model in CLAUDE_MODELS]
        if provider.lower() == "codex" and self.codex is not None:
            return self.codex.list_models()
        raise ValueError(f"unsupported runtime provider: {provider}")

    def _set_state(self, agent_id: str, state: str, **_kwargs) -> int:
        db = self._db()
        try:
            cur = db.execute(
                "UPDATE agents SET state=?, pid=NULL, last_active_at=? WHERE id=?",
                (state, now_iso(), agent_id))
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def reconcile(self) -> List[str]:
        out = list(self.claude.reconcile())
        if self.codex is not None:
            out += self.codex.reconcile()
        return out

    def shutdown(self, preserve_sessions: bool = False) -> None:
        self.claude.shutdown(preserve_sessions=preserve_sessions)
        if self.codex is not None:
            self.codex.shutdown(preserve_sessions=preserve_sessions)
