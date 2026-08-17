from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class WorkerControl:
    def __init__(self, project: Path):
        self.path = project.resolve() / ".vibe" / "tmp" / "workers.yaml"

    def get_limit(self, default: int = 8) -> int:
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            limit = payload.get("max_workers") if isinstance(payload, dict) else None
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                return default
            return limit
        except (OSError, UnicodeError, yaml.YAMLError):
            return default

    def set_limit(self, limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("Количество воркеров должно быть целым неотрицательным числом")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump({"max_workers": limit}, sort_keys=False, allow_unicode=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class DeliverySessionControl:
    """Read the optional active Delivery session selected by process management."""

    def __init__(self, project: Path):
        self.path = project.resolve() / ".vibe" / "tmp" / "delivery-session.yaml"

    def get_participants(self) -> set[str] | None:
        """Return participant IDs, or ``None`` when no active session exists.

        A missing file and an explicitly inactive session use legacy mode. An
        active but malformed session fails closed by returning no participants.
        """
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, yaml.YAMLError):
            return set()
        if isinstance(payload, dict):
            if payload.get("active") is not True:
                return None
        else:
            return set()
        participants = payload.get("participants")
        if not isinstance(participants, list) or any(not isinstance(item, str) or not item for item in participants):
            return set()
        return set(participants)


class SessionError(ValueError):
    """Ошибка проверки операции Delivery-сессии."""


def _session_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeliverySession:
    id: str
    title: str
    status: str = "draft"
    participants: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_session_now)
    updated_at: str = field(default_factory=_session_now)
    override_reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliverySession":
        allowed = {name for name in cls.__dataclass_fields__}
        payload = {key: value for key, value in data.items() if key in allowed}
        participants = payload.get("participants", [])
        payload["participants"] = list(participants) if isinstance(participants, list) else []
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if getattr(self, name) is not None}


class DeliverySessionStore:
    """Хранилище и lifecycle-проверки управляемых Delivery-сессий."""

    STATUSES = {"draft", "active", "completed", "cancelled"}

    def __init__(self, project: Path):
        self.project = project.resolve()
        self.root = self.project / ".vibe" / "tmp"
        self.path = self.root / "delivery-sessions.yaml"
        self.active_path = self.root / "delivery-session.yaml"

    def _load(self) -> list[DeliverySession]:
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SessionError(f"Не удалось прочитать хранилище сессий: {exc}") from exc
        items = payload.get("sessions", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            raise SessionError("Хранилище сессий повреждено: sessions должен быть списком")
        return [DeliverySession.from_dict(item) for item in items if isinstance(item, dict)]

    def _save(self, sessions: list[DeliverySession]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump({"sessions": [session.to_dict() for session in sessions]}, sort_keys=False, allow_unicode=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def list(self) -> list[DeliverySession]:
        return self._load()

    def get(self, session_id: str) -> DeliverySession:
        for session in self._load():
            if session.id == session_id:
                return session
        raise SessionError(f"Сессия не найдена: {session_id}")

    def create(self, title: str) -> DeliverySession:
        title = title.strip()
        if not title:
            raise SessionError("Название сессии не может быть пустым")
        sessions = self._load()
        session = DeliverySession(id=f"SES-{uuid.uuid4().hex[:6].upper()}", title=title)
        sessions.append(session)
        self._save(sessions)
        return session

    def _replace(self, changed: DeliverySession) -> DeliverySession:
        sessions = self._load()
        for index, session in enumerate(sessions):
            if session.id == changed.id:
                changed.updated_at = _session_now()
                sessions[index] = changed
                self._save(sessions)
                return changed
        raise SessionError(f"Сессия не найдена: {changed.id}")

    def _active(self) -> DeliverySession | None:
        return next((session for session in self._load() if session.status == "active"), None)

    def add(self, session_id: str, ticket_id: str, ticket_store: Any) -> DeliverySession:
        session = self.get(session_id)
        if session.status != "draft":
            raise SessionError("Участников можно менять только у черновика сессии")
        try:
            ticket = ticket_store.get(ticket_id)
        except KeyError as exc:
            raise SessionError(f"Тикет не найден: {ticket_id}") from exc
        if ticket.process != "delivery":
            raise SessionError("В Delivery-сессию можно добавлять только Delivery-тикеты")
        if ticket_store.is_done(ticket):
            raise SessionError("Завершенный тикет нельзя добавить в сессию")
        active = self._active()
        if active and ticket_id in active.participants:
            raise SessionError(f"Тикет уже входит в активную сессию {active.id}")
        if ticket_id not in session.participants:
            session.participants.append(ticket_id)
        return self._replace(session)

    def remove(self, session_id: str, ticket_id: str) -> DeliverySession:
        session = self.get(session_id)
        if session.status != "draft":
            raise SessionError("Участников можно менять только у черновика сессии")
        if ticket_id not in session.participants:
            raise SessionError(f"Тикет не входит в сессию: {ticket_id}")
        session.participants.remove(ticket_id)
        return self._replace(session)

    def activate(self, session_id: str, ticket_store: Any) -> DeliverySession:
        session = self.get(session_id)
        if session.status != "draft":
            raise SessionError("Активировать можно только черновик сессии")
        if not session.participants:
            raise SessionError("Нельзя активировать пустую сессию")
        active = self._active()
        if active:
            raise SessionError(f"Уже есть активная сессия: {active.id}")
        participants = []
        for ticket_id in session.participants:
            try:
                ticket = ticket_store.get(ticket_id)
            except KeyError as exc:
                raise SessionError(f"Участник не найден: {ticket_id}") from exc
            if ticket.process != "delivery" or ticket_store.is_done(ticket):
                raise SessionError(f"Некорректный состав сессии: {ticket_id}")
            participants.append(ticket)

        for ticket in participants:
            if ticket.status == "todo":
                ticket.status = "selected_for_session"
                ticket_store.save(ticket)
        session.status = "active"
        self._replace(session)
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        self.active_path.write_text(yaml.safe_dump({"active": True, "session_id": session.id, "participants": session.participants}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return session

    def _finish(self, session_id: str, status: str, ticket_store: Any, reason: str | None) -> DeliverySession:
        session = self.get(session_id)
        if session.status != "active":
            raise SessionError(f"Завершить можно только активную сессию (сейчас: {session.status})")
        incomplete = []
        for ticket_id in session.participants:
            try:
                ticket = ticket_store.get(ticket_id)
            except KeyError:
                incomplete.append(ticket_id)
                continue
            if not ticket_store.is_done(ticket):
                incomplete.append(ticket_id)
        reason = reason.strip() if isinstance(reason, str) else None
        if incomplete and not reason:
            raise SessionError("Сессия неполная; укажите --override с причиной")
        session.status = status
        session.override_reason = reason
        self._replace(session)
        if status == "completed" or status == "cancelled":
            self.active_path.unlink(missing_ok=True)
        return session

    def complete(self, session_id: str, ticket_store: Any, reason: str | None = None) -> DeliverySession:
        return self._finish(session_id, "completed", ticket_store, reason)

    def cancel(self, session_id: str, ticket_store: Any, reason: str | None = None) -> DeliverySession:
        return self._finish(session_id, "cancelled", ticket_store, reason)
