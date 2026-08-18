from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .sessions import SessionStore


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
    """Read active participants from the persistent session model."""

    def __init__(self, project: Path):
        self.project = project.resolve()
        self.path = self.project / ".vibe" / "tmp" / "delivery-session.yaml"
        self.store = SessionStore(self.project)

    def get_participants(self) -> set[str] | None:
        """Return participant IDs, or ``None`` when no active session exists.

        A missing file and an explicitly inactive session use legacy mode. An
        active but malformed session fails closed by returning no participants.
        """
        try:
            active = next((session for session in self.store.list() if session.status == "active"), None)
            if active is not None:
                return self.store.effective_ticket_ids(active)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            return set()
        # Keep reading the marker for projects that have not yet got a
        # versioned session document; it is only a migration fallback.
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


class DeliverySessionStore:
    """Compatibility adapter backed exclusively by the persistent SessionStore."""

    def __init__(self, project: Path):
        self.project = project.resolve()
        self.store = SessionStore(self.project)
        self.root = self.project / ".vibe" / "tmp"
        self.active_path = self.root / "delivery-session.yaml"

    def list(self):
        try:
            return self.store.list()
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(str(exc)) from exc

    def get(self, session_id: str):
        if session_id.startswith("SES-"):
            session_id = f"SESSION-{session_id[4:]}"
        try:
            return self.store.get(session_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(f"Сессия не найдена: {session_id}") from exc

    def create(self, title: str, *, budget_policy: str = "legacy", budget_limits: dict[str, int | None] | None = None,
               membership_policy: str = "legacy"):
        if not isinstance(title, str) or not title.strip():
            raise SessionError("Название сессии не может быть пустым")
        try:
            return self.store.create(title=title.strip(), budget_policy=budget_policy, budget_limits=budget_limits,
                                     membership_policy=membership_policy)
        except (TypeError, ValueError) as exc:
            raise SessionError(str(exc)) from exc

    def include(self, session_id: str, ticket_id: str, ticket_store: Any):
        return self.add(session_id, ticket_id, ticket_store)

    def override(self, session_id: str, ticket_id: str, *, actor: str, reason: str):
        session = self.get(session_id)
        try:
            self.store.override_ticket(session, ticket_id, actor=actor, reason=reason)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(str(exc)) from exc
        return session

    def _replace(self, changed):
        try:
            self.store.save(changed)
            return changed
        except (KeyError, TypeError, ValueError) as exc:
            # Keep the adapter able to inspect malformed legacy membership so
            # activation can fail closed without changing ticket state.
            if "Unknown ticket:" in str(exc):
                self.store.session_path(changed).write_text(
                    yaml.safe_dump(changed.to_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8"
                )
                return changed
            raise SessionError(str(exc)) from exc

    def _active(self):
        return next((session for session in self.list() if session.status == "active"), None)

    def add(self, session_id: str, ticket_id: str, ticket_store: Any):
        session = self.get(session_id)
        try:
            ticket = ticket_store.get(ticket_id)
        except KeyError as exc:
            raise SessionError(f"Тикет не найден: {ticket_id}") from exc
        if ticket.process != "delivery":
            raise SessionError("В Delivery-сессию можно добавлять только Delivery-тикеты")
        if ticket_store.is_done(ticket):
            raise SessionError("Завершенный тикет нельзя добавить в сессию")
        active = self._active()
        if active and ticket_id in active.ticket_ids:
            raise SessionError(f"Тикет уже входит в активную сессию {active.id}")
        try:
            self.store.add_ticket(session, ticket_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(str(exc)) from exc
        return session

    def remove(self, session_id: str, ticket_id: str):
        session = self.get(session_id)
        try:
            self.store.remove_ticket(session, ticket_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(str(exc)) from exc
        return session

    def activate(self, session_id: str, ticket_store: Any):
        session = self.get(session_id)
        if self._active() is not None:
            raise SessionError("Уже есть активная сессия")
        try:
            participants = []
            for ticket_id in session.ticket_ids:
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
            self.store.activate(session)
        except SessionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            message = str(exc)
            if message == "Cannot activate an empty session":
                message = "Нельзя активировать пустую сессию"
            raise SessionError(message) from exc
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        marker = {"active": True, "session_id": session.id, "participants": session.ticket_ids}
        if session.budget_policy != "legacy" or session.membership_policy != "legacy" or any(
                value is not None for value in session.budget_limits.values()):
            marker.update({"budget_policy": session.budget_policy, "budget_limits": session.budget_limits,
                           "membership_policy": session.membership_policy})
        self.active_path.write_text(
            yaml.safe_dump(marker, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return session

    def _finish(self, session_id: str, status: str, ticket_store: Any, reason: str | None):
        session = self.get(session_id)
        if session.status != "active":
            raise SessionError(f"Завершить можно только активную сессию (сейчас: {session.status})")
        incomplete = []
        for ticket_id in session.ticket_ids:
            try:
                if not ticket_store.is_done(ticket_store.get(ticket_id)):
                    incomplete.append(ticket_id)
            except KeyError:
                incomplete.append(ticket_id)
        reason = reason.strip() if isinstance(reason, str) else None
        if incomplete and not reason:
            raise SessionError("Сессия неполная; укажите --override с причиной")
        try:
            (self.store.complete if status == "completed" else self.store.cancel)(session)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(str(exc)) from exc
        self.active_path.unlink(missing_ok=True)
        return session

    def complete(self, session_id: str, ticket_store: Any, reason: str | None = None):
        return self._finish(session_id, "completed", ticket_store, reason)

    def cancel(self, session_id: str, ticket_store: Any, reason: str | None = None):
        return self._finish(session_id, "cancelled", ticket_store, reason)
