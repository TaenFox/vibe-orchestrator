from __future__ import annotations

import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - supported deployment targets are POSIX
    fcntl = None  # type: ignore[assignment]

from .tickets import TicketStore


SCHEMA_VERSION = 1
SESSION_STATUSES = {"draft", "active", "completed", "cancelled"}
OPEN_STATUSES = {"draft", "active"}
DELIVERY_TICKET_TYPES = {"story", "task", "bug", "rework"}
SESSION_ID_PATTERN = re.compile(r"SESSION-[A-Z0-9]+\Z")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeliverySession:
    id: str
    title: str = ""
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
        had_created_at = bool(payload.get("created_at"))
        payload.setdefault("created_at", now_iso())
        # Legacy documents used ``created_at`` as their only lifecycle
        # timestamp. Treat them as an explicit migration rather than as
        # corrupt documents; the next save writes the complete schema.
        if "updated_at" not in payload and had_created_at:
            payload["updated_at"] = payload["created_at"]
        else:
            payload.setdefault("updated_at", None)
        payload.setdefault("started_at", None)
        payload.setdefault("completed_at", None)
        payload.setdefault("cancelled_at", None)
        events = payload.pop("events", payload.get("audit_events", []))
        payload["audit_events"] = [dict(item) for item in events if isinstance(item, dict)] if isinstance(events, list) else []
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    @property
    def participants(self) -> list[str]:
        """Compatibility name used by the delivery UI and API."""
        return self.ticket_ids

    @participants.setter
    def participants(self, value: list[str]) -> None:
        self.ticket_ids = value

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class SessionStore:
    """Persistent delivery-session state under ``.vibe/sessions``."""

    def __init__(self, project: Path, ticket_store: TicketStore | None = None):
        self.project = project.resolve()
        self.root = self.project / ".vibe"
        self.sessions_root = self.root / "sessions"
        self.lock_path = self.root / "sessions.lock"
        self.ticket_store = ticket_store or TicketStore(self.project)
        self._migrating = False

    def init(self) -> None:
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy()

    def session_path(self, session: DeliverySession | str) -> Path:
        session_id = session.id if isinstance(session, DeliverySession) else session
        self._validate_session_id(session_id)
        return self.sessions_root / f"{session_id}.yaml"

    def create(self, ticket_ids: list[str] | None = None, *, title: str = "") -> DeliverySession:
        session = DeliverySession(id=f"SESSION-{uuid.uuid4().hex[:12].upper()}", title=title)
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
        path = self._validate_session_path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Invalid session document: {path}")
        session = DeliverySession.from_dict(data)
        self._validate_session_id(session.id)
        if path.stem != session.id:
            raise ValueError(f"Session ID does not match file name: {path}")
        self._validate_lifecycle(session, check_updated_order=True)
        return session

    def list(self) -> list[DeliverySession]:
        self.init()
        return [self.load_path(path) for path in sorted(self.sessions_root.glob("*.yaml"))]

    def save(self, session: DeliverySession) -> None:
        path = self._validate_session_path(self.session_path(session))
        with self._save_lock():
            persisted = self.load_path(path) if path.exists() else None
            if persisted is not None:
                self._validate_transition(persisted, session)
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

    def _migrate_legacy(self) -> None:
        """Import the pre-versioned aggregate store once into session files."""
        legacy_path = self.root / "tmp" / "delivery-sessions.yaml"
        if self._migrating or not legacy_path.exists() or any(self.sessions_root.glob("*.yaml")):
            return
        self._migrating = True
        try:
            self._migrate_legacy_documents(legacy_path)
        finally:
            self._migrating = False

    def _migrate_legacy_documents(self, legacy_path: Path) -> None:
        try:
            payload = yaml.safe_load(legacy_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"Unable to migrate legacy sessions: {exc}") from exc
        items = payload.get("sessions", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            raise ValueError("Unable to migrate legacy sessions: sessions must be a list")
        active_payload = {}
        active_path = self.root / "tmp" / "delivery-session.yaml"
        if active_path.exists():
            try:
                active_payload = yaml.safe_load(active_path.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise ValueError(f"Unable to migrate legacy active session: {exc}") from exc
        active_id = active_payload.get("session_id") if isinstance(active_payload, dict) else None
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            old_id = item["id"]
            suffix = old_id.removeprefix("SES-")
            session_id = f"SESSION-{suffix}" if old_id.startswith("SES-") else old_id
            if not SESSION_ID_PATTERN.fullmatch(session_id):
                continue
            created_at = item.get("created_at") or now_iso()
            status = item.get("status", "draft")
            if old_id == active_id and status == "draft":
                status = "active"
            session = DeliverySession(
                id=session_id,
                title=str(item.get("title", "")),
                status=status,
                ticket_ids=list(item.get("participants", [])) if isinstance(item.get("participants", []), list) else [],
                created_at=created_at,
                updated_at=item.get("updated_at") or created_at,
                audit_events=[{"event": "migrated", "timestamp": now_iso(), "legacy_id": old_id}],
            )
            if status == "active":
                session.started_at = session.updated_at
            elif status == "completed":
                session.started_at = session.updated_at
                session.completed_at = session.updated_at
            elif status == "cancelled":
                session.cancelled_at = session.updated_at
            self.save(session)

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
        self._validate_session_id(session.id)
        if session.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported session schema version: {session.schema_version!r}")
        if session.status not in SESSION_STATUSES:
            raise ValueError(f"Invalid session status: {session.status!r}")
        self._validate_membership(session, session.ticket_ids)
        self._validate_lifecycle(session)
        if session.status in OPEN_STATUSES:
            for other in self.list():
                if other.id == session.id or other.status not in OPEN_STATUSES:
                    continue
                overlap = set(session.ticket_ids) & set(other.ticket_ids)
                if overlap:
                    raise ValueError(f"Ticket already belongs to an open session: {sorted(overlap)[0]}")

    def _validate_lifecycle(self, session: DeliverySession, *, check_updated_order: bool = False) -> None:
        if session.status == "active" and not session.ticket_ids:
            raise ValueError("Active sessions cannot be empty")
        if session.status == "draft":
            expected = (None, None, None)
        elif session.status == "active":
            expected = (session.started_at, None, None)
        elif session.status == "completed":
            expected = (session.started_at, session.completed_at, None)
        else:
            expected = (session.started_at, None, session.cancelled_at)

        if session.status == "active" and not expected[0]:
            raise ValueError("Active sessions require started_at")
        if session.status == "completed" and not expected[0]:
            raise ValueError("Completed sessions require started_at")
        if session.status == "completed" and not expected[1]:
            raise ValueError("Completed sessions require completed_at")
        if session.status == "cancelled" and not expected[2]:
            raise ValueError("Cancelled sessions require cancelled_at")
        if session.status == "completed" and session.cancelled_at:
            raise ValueError("Completed sessions cannot have cancelled_at")
        if session.status == "cancelled" and session.completed_at:
            raise ValueError("Cancelled sessions cannot have completed_at")
        if session.status in {"draft", "active"} and (session.completed_at or session.cancelled_at):
            raise ValueError("Open sessions cannot have terminal timestamps")
        if session.status == "draft" and session.started_at:
            raise ValueError("Draft sessions cannot have started_at")

        timestamps = {
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "cancelled_at": session.cancelled_at,
        }
        if session.updated_at is None:
            raise ValueError("Session updated_at is required")
        parsed_timestamps = {}
        for name, value in timestamps.items():
            if value is not None:
                try:
                    parsed_timestamps[name] = datetime.fromisoformat(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Session timestamps must be ISO-8601") from exc
        try:
            if check_updated_order:
                for name, timestamp in parsed_timestamps.items():
                    if name != "updated_at" and timestamp > parsed_timestamps["updated_at"]:
                        raise ValueError(f"updated_at cannot precede {name}")
            if parsed_timestamps.get("started_at") and parsed_timestamps.get("completed_at") is not None and parsed_timestamps["completed_at"] < parsed_timestamps["started_at"]:
                raise ValueError("completed_at cannot precede started_at")
            if parsed_timestamps.get("started_at") and parsed_timestamps.get("cancelled_at") is not None and parsed_timestamps["cancelled_at"] < parsed_timestamps["started_at"]:
                raise ValueError("cancelled_at cannot precede started_at")
        except TypeError as exc:
            raise ValueError("Session timestamps must use compatible ISO-8601 offsets") from exc

    def _validate_transition(self, persisted: DeliverySession, session: DeliverySession) -> None:
        if persisted.id != session.id:
            raise ValueError("Session ID cannot be changed")
        if persisted.schema_version != session.schema_version:
            raise ValueError("Session schema version cannot be changed")
        if persisted.created_at != session.created_at:
            raise ValueError("Session creation time cannot be changed")

        allowed = {
            "draft": {"draft", "active", "cancelled"},
            "active": {"active", "completed", "cancelled"},
            "completed": {"completed"},
            "cancelled": {"cancelled"},
        }
        if session.status not in allowed.get(persisted.status, set()):
            raise ValueError(f"Invalid session status transition: {persisted.status!r} -> {session.status!r}")
        if persisted.status != "draft" and persisted.ticket_ids != session.ticket_ids:
            raise ValueError("Session membership can only be changed in draft")
        if persisted.status == "active" and session.started_at != persisted.started_at:
            raise ValueError("Session start time cannot be changed")
        if persisted.status in {"completed", "cancelled"}:
            for field_name in ("started_at", "completed_at", "cancelled_at"):
                if getattr(session, field_name) != getattr(persisted, field_name):
                    raise ValueError("Terminal session timestamps cannot be changed")
        if persisted.status == "draft" and session.status == "draft":
            if any(getattr(session, field_name) for field_name in ("started_at", "completed_at", "cancelled_at")):
                raise ValueError("Draft sessions cannot have lifecycle timestamps")

        if session.status == "cancelled" and persisted.status == "draft":
            if session.started_at is not None or session.completed_at is not None:
                raise ValueError("Cancelled draft sessions cannot have started_at or completed_at")
        if session.status == "cancelled" and persisted.status == "active" and session.completed_at is not None:
            raise ValueError("Cancelled active sessions cannot have completed_at")

        if session.status == "cancelled" and persisted.status == "active" and not session.started_at:
            raise ValueError("Cancelled active sessions require started_at")

    @contextmanager
    def _save_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

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

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ValueError("Session ID must match SESSION-[A-Z0-9]+")

    def _validate_session_path(self, path: Path) -> Path:
        candidate = Path(path)
        root = self.sessions_root.resolve()
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("Session path must be inside .vibe/sessions") from exc
        if len(relative.parts) != 1 or relative.suffix != ".yaml":
            raise ValueError("Session path must be a session YAML file")
        self._validate_session_id(relative.stem)
        return resolved
