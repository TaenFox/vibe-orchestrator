"""Read-only data tools exposed to workers.

The functions in this module deliberately bypass mutating store helpers.  An
agent may inspect control-plane state, but a read must not create directories,
perform migrations, or write a cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from .sessions import DeliverySession, SessionStore
from .tickets import Ticket, TicketStore, TicketWriteService

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


def _bounded(value: int | None, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("limit must be a positive integer")
    return min(value, maximum)


def _page(items: list[dict[str, Any]], *, offset: int, limit: int) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    selected = items[offset:offset + limit]
    return {
        "contract_version": "agent.read.v1",
        "items": selected,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(items),
        "total": len(items),
    }


def _url_path(*parts: str) -> str:
    return "/artifacts/" + "/".join(quote(part, safe="") for part in parts)


def _files_below(candidate: Path, root: Path, *, recursive: bool) -> list[Path]:
    if not candidate.is_dir():
        return [candidate] if candidate.is_file() and root in candidate.resolve().parents else []
    paths = candidate.rglob("*") if recursive else candidate.iterdir()
    files = []
    for path in paths:
        if not path.is_file():
            continue
        resolved = path.resolve()
        if root in resolved.parents:
            files.append(path)
    return sorted(files)


def _safe_run_root(runs_root: Path, run_id: Any) -> Path | None:
    """Return a canonical run directory only for a direct child run_id."""
    if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id:
        return None
    run_parts = Path(run_id).parts
    if len(run_parts) != 1 or run_parts[0] in {".", ".."}:
        return None

    run_root = (runs_root / run_id).resolve()
    if runs_root not in run_root.parents:
        return None
    return run_root


def _artifact_links(project: Path, entry: dict[str, Any], *, source: bool = False) -> dict[str, Any] | None:
    run_id = entry.get("run_id")
    field = "source_artifacts" if source else "artifacts_path"
    artifact_path = entry.get(field)
    runs_root = (project / ".vibe" / "runs").resolve()
    run_root = _safe_run_root(runs_root, run_id)
    if run_root is None:
        return {"path": artifact_path, "links": []} if source else None
    if not isinstance(artifact_path, str):
        return {"path": artifact_path, "links": []} if source else None
    path_parts = Path(artifact_path).parts
    if Path(artifact_path).is_absolute() or ".." in path_parts:
        return {"path": artifact_path, "links": []}

    candidate = (project / artifact_path).resolve()
    allowed_root = run_root if source else runs_root
    if allowed_root not in candidate.parents and candidate != allowed_root:
        return {"path": artifact_path, "links": []}
    # Non-source artifacts may be directories, but they still belong to the
    # run named by the entry.  Reject a path from another run before computing
    # its relative path from this run's root.
    if not source and run_root not in candidate.parents and candidate != run_root:
        return {"path": artifact_path, "links": []}
    files = _files_below(candidate, allowed_root, recursive=source)
    links = []
    for path in files:
        if source or candidate.is_dir():
            relative_root = allowed_root if source else candidate
        else:
            relative_root = run_root
        relative = path.resolve().relative_to(relative_root)
        links.append(_url_path(run_id, *relative.parts))
    return {"path": artifact_path, "links": links}


def _ticket_payload(store: TicketStore, ticket: Ticket, *, history_limit: int) -> dict[str, Any]:
    payload = ticket.to_dict()
    history = [dict(item) for item in payload.get("run_history", [])]
    history = history[-history_limit:]
    for entry in history:
        artifacts = _artifact_links(store.project, entry)
        if artifacts is not None:
            entry["artifacts"] = artifacts
        # These optional fields are used by integrations that attach source
        # files to a run. Normalize them to the same safe link contract.
        source = entry.get("source_artifacts") or entry.get("source_artifact_path")
        if source:
            source_entry = dict(entry, source_artifacts=source)
            entry["source_artifacts"] = _artifact_links(store.project, source_entry, source=True)
    payload["run_history"] = history
    payload["run_history_truncated"] = len(ticket.run_history) > len(history)
    payload["audit_events"] = [dict(event) for event in ticket.audit_events[-MAX_HISTORY_LIMIT:]]
    payload["audit_events_truncated"] = len(ticket.audit_events) > MAX_HISTORY_LIMIT
    return payload


def _session_payload(store: TicketStore, session: DeliverySession) -> dict[str, Any]:
    effective = sorted(SessionStore.effective_ticket_ids(session))
    participants = list(session.ticket_ids)
    return {
        "id": session.id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
        "cancelled_at": session.cancelled_at,
        "participants": participants,
        "membership_priorities": {ticket_id: session.membership_priorities.get(ticket_id, 100)
                                   for ticket_id in participants},
        "audit_events": [dict(event) for event in session.audit_events],
        "effective_membership": effective,
        "membership_policy": session.membership_policy,
        "budget_policy": session.budget_policy,
        "budget_limits": dict(session.budget_limits),
    }


class ReadOnlyAgentTools:
    """Safe, bounded queries over one project's local control-plane state."""

    def __init__(self, project: Path, *, use_database: bool = True):
        self.project = project.resolve()
        self._store = TicketStore(self.project, use_database=use_database)
        self.sessions = SessionStore(self.project, self._store, use_database=use_database)

    def _sessions(self) -> list[DeliverySession]:
        return self.sessions.list()

    def list_tickets(self, *, process: str | None = None, status: str | None = None,
                     parent: str | None = None, session: str | None = None,
                     offset: int = 0, limit: int | None = None,
                     history_limit: int | None = None) -> dict[str, Any]:
        limit = _bounded(limit, default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
        history_limit = _bounded(history_limit, default=DEFAULT_HISTORY_LIMIT, maximum=MAX_HISTORY_LIMIT)
        session_ids: set[str] | None = None
        if session is not None:
            selected = next((item for item in self._sessions() if item.id == session), None)
            if selected is None:
                raise KeyError(session)
            session_ids = SessionStore.effective_ticket_ids(selected)
        tickets = self._store.list(process)
        filtered = [ticket for ticket in tickets if
                    (status is None or ticket.status == status) and
                    (parent is None or ticket.parent == parent) and
                    (session_ids is None or ticket.id in session_ids)]
        return _page([_ticket_payload(self._store, ticket, history_limit=history_limit) for ticket in filtered], offset=offset, limit=limit)

    def get_ticket(self, ticket_id: str, *, history_limit: int | None = None) -> dict[str, Any]:
        history_limit = _bounded(history_limit, default=DEFAULT_HISTORY_LIMIT, maximum=MAX_HISTORY_LIMIT)
        return _ticket_payload(self._store, self._store.get(ticket_id), history_limit=history_limit)

    def list_sessions(self, *, status: str | None = None, offset: int = 0,
                      limit: int | None = None) -> dict[str, Any]:
        limit = _bounded(limit, default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
        sessions = [_session_payload(self._store, item) for item in self._sessions()]
        if status is not None:
            sessions = [item for item in sessions if item["status"] == status]
        return _page(sessions, offset=offset, limit=limit)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return _session_payload(self._store, self.sessions.get(session_id))


class AgentTicketTools(ReadOnlyAgentTools):
    """Validated ticket writes; the underlying TicketStore is not exposed."""

    def __init__(self, project: Path, *, actor: str, use_database: bool = True):
        super().__init__(project, use_database=use_database)
        self.actor = actor
        self.service = TicketWriteService(self.project)

    def create_ticket(self, **data: Any) -> dict[str, Any]:
        result = self.service.create_ticket_result(data, actor=self.actor)
        if result.status == "ambiguous":
            return {"contract_version": "agent.write.v1", "deduplication": {
                "status": "ambiguous", "candidates": [dict(item) for item in result.candidates]}}
        assert result.ticket is not None
        payload = _ticket_payload(self.service.store, result.ticket, history_limit=DEFAULT_HISTORY_LIMIT)
        payload["deduplication"] = {"status": "created" if result.status == "created" else "exact",
                                    "existing": result.status == "exact"}
        return payload

    def update_ticket(self, ticket_id: str, **data: Any) -> dict[str, Any]:
            return _ticket_payload(self.service.store, self.service.update_ticket(ticket_id, data, actor=self.actor), history_limit=DEFAULT_HISTORY_LIMIT)


WriteAgentTools = AgentTicketTools


class AgentSessionTools(ReadOnlyAgentTools):
    """Agent-safe draft-session membership mutations.

    Lifecycle operations and the underlying SessionStore remain deliberately
    unavailable through this boundary.
    """

    def add_to_session(self, session_id: str, ticket_id: str, *, actor: str, origin: str,
                       priority: int | None = None) -> dict[str, Any]:
        return _session_payload(self._store, self.sessions.agent_add_ticket(
            session_id, ticket_id, actor=actor, origin=origin, priority=priority)) | {
                "contract_version": "agent.session.write.v1"}

    def remove_from_session(self, session_id: str, ticket_id: str, *, actor: str,
                            origin: str) -> dict[str, Any]:
        return _session_payload(self._store, self.sessions.agent_remove_ticket(
            session_id, ticket_id, actor=actor, origin=origin)) | {
                "contract_version": "agent.session.write.v1"}

    def update_session_membership(self, session_id: str, members: list[dict[str, Any]], *,
                                  actor: str, origin: str) -> dict[str, Any]:
        return _session_payload(self._store, self.sessions.agent_update_membership(
            session_id, members, actor=actor, origin=origin)) | {
                "contract_version": "agent.session.write.v1"}


def create_ticket(project: Path, *, actor: str, **data: Any) -> dict[str, Any]:
    return AgentTicketTools(project, actor=actor).create_ticket(**data)


def update_ticket(project: Path, ticket_id: str, *, actor: str, **data: Any) -> dict[str, Any]:
    return AgentTicketTools(project, actor=actor).update_ticket(ticket_id, **data)


def list_tickets(project: Path, **filters: Any) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).list_tickets(**filters)


def get_ticket(project: Path, ticket_id: str, **options: Any) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).get_ticket(ticket_id, **options)


def list_sessions(project: Path, **filters: Any) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).list_sessions(**filters)


def get_session(project: Path, session_id: str) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).get_session(session_id)


def add_to_session(project: Path, session_id: str, ticket_id: str, *, actor: str,
                   origin: str, priority: int | None = None) -> dict[str, Any]:
    return AgentSessionTools(project).add_to_session(session_id, ticket_id, actor=actor,
                                                     origin=origin, priority=priority)


def remove_from_session(project: Path, session_id: str, ticket_id: str, *, actor: str,
                        origin: str) -> dict[str, Any]:
    return AgentSessionTools(project).remove_from_session(session_id, ticket_id, actor=actor,
                                                          origin=origin)


def update_session_membership(project: Path, session_id: str, members: list[dict[str, Any]], *,
                              actor: str, origin: str) -> dict[str, Any]:
    return AgentSessionTools(project).update_session_membership(session_id, members, actor=actor,
                                                                origin=origin)
