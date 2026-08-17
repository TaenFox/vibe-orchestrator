from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .tickets import TicketStore


SCHEMA_VERSION = 1
SESSION_STATUSES = {"draft", "active", "completed", "cancelled"}
OPEN_STATUSES = {"draft", "active"}
DELIVERY_TICKET_TYPES = {"story", "task", "bug", "rework"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeliverySession:
    id: str
    schema_version: int = SCHEMA_VERSION
    status: str = "draft"
    ticket_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    audit_events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliverySession":
        payload = dict(data)
        # Sessions created before the versioned model had no audit trail.
        payload.setdefault("schema_version", 1)
        payload.setdefault("status", "draft")
        payload.setdefault("ticket_ids", [])
        payload.setdefault("created_at", now_iso())
        payload.setdefault("updated_at", payload["created_at"])
        payload.setdefault("started_at", None)
        payload.setdefault("completed_at", None)
        payload.setdefault("cancelled_at", None)
        events = payload.pop("events", payload.get("audit_events", []))
        payload["audit_events"] = [dict(item) for item in events if isinstance(item, dict)] if isinstance(events, list) else []
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class SessionStore:
    """Persistent delivery-session state under ``.vibe/sessions``."""

    def __init__(self, project: Path, ticket_store: TicketStore | None = None):
        self.project = project.resolve()
        self.root = self.project / ".vibe"
        self.sessions_root = self.root / "sessions"
        self.ticket_store = ticket_store or TicketStore(self.project)

    def init(self) -> None:
        self.sessions_root.mkdir(parents=True, exist_ok=True)

    def session_path(self, session: DeliverySession | str) -> Path:
        session_id = session.id if isinstance(session, DeliverySession) else session
        return self.sessions_root / f"{session_id}.yaml"

    def create(self, ticket_ids: list[str] | None = None) -> DeliverySession:
        session = DeliverySession(id=f"SESSION-{uuid.uuid4().hex[:12].upper()}")
        if ticket_ids is not None:
            if not isinstance(ticket_ids, list):
                raise TypeError("ticket_ids must be a list")
            self._validate_membership(session, ticket_ids)
            session.ticket_ids = list(ticket_ids)
        self._audit(session, "created")
        self.save(session)
        return session

    def get(self, session_id: str) -> DeliverySession:
        path = self.session_path(session_id)
        if not path.exists():
            raise KeyError(session_id)
        return self.load_path(path)

    def load_path(self, path: Path) -> DeliverySession:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Invalid session document: {path}")
        return DeliverySession.from_dict(data)

    def list(self) -> list[DeliverySession]:
        return [self.load_path(path) for path in sorted(self.sessions_root.glob("*.yaml"))]

    def save(self, session: DeliverySession) -> None:
        path = self.session_path(session)
        if path.exists():
            persisted = self.load_path(path)
            if persisted.status != "draft" and persisted.ticket_ids != session.ticket_ids:
                raise ValueError("Session membership can only be changed in draft")
        self._validate_session(session)
        session.updated_at = now_iso()
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(session.to_dict(), sort_keys=False, allow_unicode=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def add_ticket(self, session: DeliverySession, ticket_id: str) -> None:
        self._require_draft(session)
        self._validate_membership(session, [ticket_id])
        if ticket_id in session.ticket_ids:
            raise ValueError(f"Ticket already belongs to session: {ticket_id}")
        session.ticket_ids.append(ticket_id)
        self._audit(session, "ticket_added", ticket_id=ticket_id)
        self.save(session)

    def remove_ticket(self, session: DeliverySession, ticket_id: str) -> None:
        self._require_draft(session)
        if ticket_id not in session.ticket_ids:
            raise KeyError(ticket_id)
        session.ticket_ids.remove(ticket_id)
        self._audit(session, "ticket_removed", ticket_id=ticket_id)
        self.save(session)

    def activate(self, session: DeliverySession) -> None:
        self._require_status(session, "draft")
        if not session.ticket_ids:
            raise ValueError("Cannot activate an empty session")
        session.status = "active"
        session.started_at = now_iso()
        self._audit(session, "activated")
        self.save(session)

    def complete(self, session: DeliverySession) -> None:
        self._require_status(session, "active")
        session.status = "completed"
        session.completed_at = now_iso()
        self._audit(session, "completed")
        self.save(session)

    def cancel(self, session: DeliverySession) -> None:
        if session.status not in {"draft", "active"}:
            raise ValueError(f"Cannot cancel session in status {session.status!r}")
        session.status = "cancelled"
        session.cancelled_at = now_iso()
        self._audit(session, "cancelled")
        self.save(session)

    def _validate_session(self, session: DeliverySession) -> None:
        if session.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported session schema version: {session.schema_version!r}")
        if session.status not in SESSION_STATUSES:
            raise ValueError(f"Invalid session status: {session.status!r}")
        self._validate_membership(session, session.ticket_ids)
        if session.status in OPEN_STATUSES:
            for other in self.list():
                if other.id == session.id or other.status not in OPEN_STATUSES:
                    continue
                overlap = set(session.ticket_ids) & set(other.ticket_ids)
                if overlap:
                    raise ValueError(f"Ticket already belongs to an open session: {sorted(overlap)[0]}")

    def _validate_membership(self, session: DeliverySession, ticket_ids: list[str]) -> None:
        if not isinstance(ticket_ids, list):
            raise TypeError("ticket_ids must be a list")
        if session.status != "draft" and ticket_ids != session.ticket_ids:
            raise ValueError("Session membership can only be changed in draft")
        if any(not isinstance(ticket_id, str) or not ticket_id for ticket_id in ticket_ids):
            raise TypeError("ticket_ids must contain non-empty strings")
        if len(ticket_ids) != len(set(ticket_ids)):
            raise ValueError("Duplicate ticket IDs are not allowed")
        for ticket_id in ticket_ids:
            try:
                ticket = self.ticket_store.get(ticket_id)
            except KeyError as exc:
                raise ValueError(f"Unknown ticket: {ticket_id}") from exc
            if ticket.process != "delivery" or ticket.type not in DELIVERY_TICKET_TYPES:
                raise ValueError(f"Invalid delivery ticket type: {ticket.type!r}")

    @staticmethod
    def _require_draft(session: DeliverySession) -> None:
        if session.status != "draft":
            raise ValueError("Session membership can only be changed in draft")

    @staticmethod
    def _require_status(session: DeliverySession, expected: str) -> None:
        if session.status != expected:
            raise ValueError(f"Expected session status {expected!r}, got {session.status!r}")

    @staticmethod
    def _audit(session: DeliverySession, event: str, **extra: Any) -> None:
        entry = {"event": event, "timestamp": now_iso()}
        entry.update(extra)
        session.audit_events.append(entry)
