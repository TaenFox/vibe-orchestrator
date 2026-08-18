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
from .tickets import Ticket, TicketStore

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


def _artifact_links(project: Path, entry: dict[str, Any]) -> dict[str, Any] | None:
    run_id = entry.get("run_id")
    artifact_path = entry.get("artifacts_path")
    if not isinstance(run_id, str) or not run_id or not isinstance(artifact_path, str):
        return None
    run_dir = (project / artifact_path).resolve()
    runs_root = (project / ".vibe" / "runs").resolve()
    if runs_root not in run_dir.parents or not run_dir.is_dir():
        return {"path": artifact_path, "links": []}
    names = [path.name for path in sorted(run_dir.iterdir()) if path.is_file()]
    return {
        "path": artifact_path,
        "links": [f"/artifacts/{quote(run_id, safe='')}/{quote(name, safe='')}" for name in names],
    }


def _ticket_payload(store: TicketStore, ticket: Ticket, *, history_limit: int) -> dict[str, Any]:
    payload = ticket.to_dict()
    history = [dict(item) for item in payload.get("run_history", [])]
    history = history[-history_limit:]
    for entry in history:
        artifacts = _artifact_links(store.project, entry)
        if artifacts is not None:
            entry["artifacts"] = artifacts
        # These optional fields are used by integrations that attach source
        # files to a run. Keep them as links only when they are present.
        source = entry.get("source_artifacts") or entry.get("source_artifact_path")
        if source:
            entry["source_artifacts"] = source
    payload["run_history"] = history
    payload["run_history_truncated"] = len(ticket.run_history) > len(history)
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
        "audit_events": [dict(event) for event in session.audit_events],
        "effective_membership": effective,
        "membership_policy": session.membership_policy,
        "budget_policy": session.budget_policy,
        "budget_limits": dict(session.budget_limits),
    }


class ReadOnlyAgentTools:
    """Safe, bounded queries over one project's local control-plane state."""

    def __init__(self, project: Path):
        self.project = project.resolve()
        self.tickets = TicketStore(self.project)
        self.sessions = SessionStore(self.project, self.tickets)

    def _sessions(self) -> list[DeliverySession]:
        root = self.sessions.sessions_root
        if not root.is_dir():
            return []
        return [self.sessions.load_path(path) for path in sorted(root.glob("*.yaml"))]

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
        tickets = self.tickets.list(process)
        filtered = [ticket for ticket in tickets if
                    (status is None or ticket.status == status) and
                    (parent is None or ticket.parent == parent) and
                    (session_ids is None or ticket.id in session_ids)]
        return _page([_ticket_payload(self.tickets, ticket, history_limit=history_limit) for ticket in filtered], offset=offset, limit=limit)

    def get_ticket(self, ticket_id: str, *, history_limit: int | None = None) -> dict[str, Any]:
        history_limit = _bounded(history_limit, default=DEFAULT_HISTORY_LIMIT, maximum=MAX_HISTORY_LIMIT)
        return _ticket_payload(self.tickets, self.tickets.get(ticket_id), history_limit=history_limit)

    def list_sessions(self, *, status: str | None = None, offset: int = 0,
                      limit: int | None = None) -> dict[str, Any]:
        limit = _bounded(limit, default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
        sessions = [_session_payload(self.tickets, item) for item in self._sessions()]
        if status is not None:
            sessions = [item for item in sessions if item["status"] == status]
        return _page(sessions, offset=offset, limit=limit)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return _session_payload(self.tickets, self.sessions.get(session_id))


def list_tickets(project: Path, **filters: Any) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).list_tickets(**filters)


def get_ticket(project: Path, ticket_id: str, **options: Any) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).get_ticket(ticket_id, **options)


def list_sessions(project: Path, **filters: Any) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).list_sessions(**filters)


def get_session(project: Path, session_id: str) -> dict[str, Any]:
    return ReadOnlyAgentTools(project).get_session(session_id)
