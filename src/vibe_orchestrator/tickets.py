from __future__ import annotations

import os
import logging
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import load_workflow

RETRY_BACKOFF_SECONDS = (5, 30)
TICKET_TYPES_BY_PROCESS = {
    "discovery": ("idea", "correction"),
    "delivery": ("story", "task", "bug", "rework"),
    "process_management": ("audit", "planning", "estimation"),
}
AGENT_CREATE_FIELDS = frozenset({"process", "type", "title", "description", "priority", "parent", "mandatory", "idempotency_key", "origin", "status"})
AGENT_UPDATE_FIELDS = frozenset({"title", "description", "priority", "parent", "blocked_by", "mandatory", "context", "origin", "expected_updated_at"})
AGENT_FORBIDDEN_UPDATE_FIELDS = frozenset({"id", "process", "type", "status", "active_run", "last_outcome", "last_summary", "consecutive_failures", "retry_after", "run_history", "created_at", "updated_at", "wip_exempt", "correction_stage", "rework_stage"})
_WRITE_LOCK = threading.RLock()
log = logging.getLogger("vibe")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Ticket:
    id: str
    process: str
    type: str
    title: str
    status: str
    priority: int = 100
    description: str = ""
    parent: str | None = None
    correction_stage: str | None = None
    rework_stage: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    mandatory: bool = True
    implementation_required: bool | None = None
    wip_exempt: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    active_run: str | None = None
    last_outcome: str | None = None
    last_summary: str | None = None
    blocked_reason: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    context_revision: int = 0
    consecutive_failures: int = 0
    retry_after: str | None = None
    run_history: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ticket":
        payload = dict(data)
        history = payload.get("run_history")
        if isinstance(history, list):
            payload["run_history"] = [dict(item) for item in history if isinstance(item, dict)]
        else:
            payload["run_history"] = []
        audit_events = payload.get("audit_events")
        payload["audit_events"] = [dict(item) for item in audit_events if isinstance(item, dict)] if isinstance(audit_events, list) else []
        context = payload.get("context")
        payload["context"] = dict(context) if isinstance(context, dict) else {}
        revision = payload.get("context_revision", 0)
        payload["context_revision"] = revision if isinstance(revision, int) and revision >= 0 else 0
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        if not payload["run_history"]:
            payload.pop("run_history")
        if not payload["audit_events"]:
            payload.pop("audit_events")
        if payload["implementation_required"] is None:
            payload.pop("implementation_required")
        if payload["correction_stage"] is None:
            payload.pop("correction_stage")
        if payload["rework_stage"] is None:
            payload.pop("rework_stage")
        if payload["consecutive_failures"] == 0:
            payload.pop("consecutive_failures")
        if payload["retry_after"] is None:
            payload.pop("retry_after")
        if payload["blocked_reason"] is None:
            payload.pop("blocked_reason")
        if not payload["context"]:
            payload.pop("context")
        if payload["context_revision"] == 0:
            payload.pop("context_revision")
        return payload


class TicketStore:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.root = self.project / ".vibe"
        self.tickets_root = self.root / "tickets"
        self.runs_root = self.root / "runs"

    def init(self) -> None:
        self.tickets_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        for process in ("discovery", "delivery", "process_management"):
            (self.tickets_root / process).mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text(
                "# .vibe\n\n"
                "Состояние тикетов для vibe-orchestrator. Каталог `tickets/` является локальным состоянием control plane и не коммитится; `runs/` содержит локальные артефакты запусков (`run.json`, `events.jsonl`, `result.json`).\n",
                encoding="utf-8",
            )
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("runs/\ntmp/\ntickets/\nsessions/\nsessions.lock\n", encoding="utf-8")
        else:
            entries = gitignore.read_text(encoding="utf-8").splitlines()
            missing = [entry for entry in ("runs/", "tmp/", "tickets/", "sessions/", "sessions.lock") if entry not in entries]
            if missing:
                gitignore.write_text("\n".join([*entries, *missing]) + "\n", encoding="utf-8")

    def ticket_path(self, ticket: Ticket) -> Path:
        return self.tickets_root / ticket.process / f"{ticket.id}.yaml"

    def save(self, ticket: Ticket) -> None:
        ticket.updated_at = now_iso()
        path = self.ticket_path(ticket)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(ticket.to_dict(), sort_keys=False, allow_unicode=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def load_path(self, path: Path) -> Ticket:
        return Ticket.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))

    def run_path(self, run_id: str) -> Path:
        return self.runs_root / run_id

    def record_run_event(
        self,
        ticket: Ticket,
        *,
        run_id: str | None,
        stage_id: str | None,
        event: str,
        **extra: Any,
    ) -> None:
        entry = {
            "event": event,
            "timestamp": now_iso(),
            "ticket_type": ticket.type,
        }
        if run_id is not None:
            entry["run_id"] = run_id
            entry["artifacts_path"] = f".vibe/runs/{run_id}"
        if stage_id is not None:
            entry["stage"] = stage_id
        entry.update({key: value for key, value in extra.items() if value is not None})
        ticket.run_history.append(entry)

    def get(self, ticket_id: str) -> Ticket:
        matches = list(self.tickets_root.glob(f"*/{ticket_id}.yaml"))
        if not matches:
            raise KeyError(ticket_id)
        return self.load_path(matches[0])

    def list(self, process: str | None = None) -> list[Ticket]:
        base = self.tickets_root / process if process else self.tickets_root
        pattern = "*.yaml" if process else "*/*.yaml"
        return [self.load_path(path) for path in sorted(base.glob(pattern))]

    def children_of(self, parent_id: str, *, process: str | None = None) -> list[Ticket]:
        tickets = [ticket for ticket in self.list(process) if ticket.parent == parent_id]
        tickets.sort(key=lambda ticket: (ticket.created_at, ticket.priority, ticket.title, ticket.id))
        return tickets

    def is_done(self, ticket: Ticket) -> bool:
        workflow = load_workflow(ticket.process)
        stage = workflow.by_id[ticket.status]
        return stage.kind == "done"

    def create(self, process: str, ticket_type: str, title: str, description: str = "", priority: int = 100, parent: str | None = None, status: str | None = None, wip_exempt: bool | None = None, mandatory: bool = True, correction_stage: str | None = None, rework_stage: str | None = None) -> Ticket:
        workflow = load_workflow(process)
        prefix = {"discovery": "DISC", "delivery": "DEL", "process_management": "PM"}[process]
        ticket_id = f"{prefix}-{uuid.uuid4().hex[:6].upper()}"
        if wip_exempt is None:
            wip_exempt = ticket_type in {"rework", "correction"}
        ticket = Ticket(id=ticket_id, process=process, type=ticket_type, title=title, status=status or workflow.initial_status, priority=priority, description=description, parent=parent, correction_stage=correction_stage, rework_stage=rework_stage, mandatory=mandatory, wip_exempt=wip_exempt)
        self.record_run_event(ticket, run_id=None, stage_id=ticket.status, event="created")
        self.save(ticket)
        log.info("создан тикет %s (%s): %s", ticket.id, ticket.type, ticket.title)
        return ticket


class TicketWriteError(ValueError):
    """Base error for the constrained agent ticket write contract."""


class TicketWriteConflict(TicketWriteError):
    pass


def _parent_is_compatible(parent: Ticket, process: str, ticket_type: str) -> bool:
    if process == "discovery":
        return ticket_type == "correction" and parent.process == "discovery" and parent.type == "idea"
    if process == "delivery":
        if ticket_type == "rework":
            return parent.process == "delivery" and parent.type != "rework"
        return (parent.process == "discovery" and parent.type == "idea") or (parent.process == "delivery" and parent.type != "rework")
    return False


def _validate_priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TicketWriteError("priority must be a non-negative integer")
    return value


def _validate_origin(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise TicketWriteError("origin must be a non-empty string of at most 200 characters")
    return value.strip()


def _validate_actor(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise TicketWriteError("actor must be a non-empty string of at most 200 characters")
    return value.strip()


def _validate_title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketWriteError("title must be a non-empty string")
    return value.strip()


def _snapshot(ticket: Ticket, fields: set[str]) -> dict[str, Any]:
    return {field: getattr(ticket, field) for field in sorted(fields)}


class TicketWriteService:
    """The only agent-facing mutation boundary for ticket metadata."""

    def __init__(self, project: Path):
        self.store = TicketStore(project)

    def _validate_common(self, data: dict[str, Any], *, update: bool = False) -> None:
        allowed = AGENT_UPDATE_FIELDS if update else AGENT_CREATE_FIELDS
        unknown = set(data) - allowed
        if unknown:
            forbidden = sorted(unknown & AGENT_FORBIDDEN_UPDATE_FIELDS)
            label = f"forbidden fields: {', '.join(forbidden)}" if forbidden else f"unknown fields: {', '.join(sorted(unknown))}"
            raise TicketWriteError(label)
        _validate_origin(data.get("origin"))

    def _validate_parent(self, ticket_id: str | None, process: str, ticket_type: str, *, current_id: str | None = None) -> None:
        if not ticket_id:
            if ticket_type in {"correction", "rework"}:
                raise TicketWriteError("parent is required for correction/rework")
            return
        if ticket_id == current_id:
            raise TicketWriteError("a ticket cannot be its own parent")
        try:
            parent = self.store.get(ticket_id)
        except KeyError as exc:
            raise TicketWriteError("parent ticket not found") from exc
        if not _parent_is_compatible(parent, process, ticket_type):
            raise TicketWriteError("parent is incompatible with process and type")
        seen: set[str] = set()
        while parent.parent:
            if parent.id in seen:
                raise TicketWriteError("parent ancestry contains a cycle")
            seen.add(parent.id)
            if parent.parent == current_id:
                raise TicketWriteError("parent change would create a cycle")
            try:
                parent = self.store.get(parent.parent)
            except KeyError:
                break

    def _validate_blocked_by(self, ticket_id: str, blocked_by: Any) -> list[str]:
        if not isinstance(blocked_by, list) or any(not isinstance(item, str) or not item.strip() for item in blocked_by):
            raise TicketWriteError("blocked_by must be a list of ticket IDs")
        values = [item.strip() for item in blocked_by]
        if len(values) != len(set(values)):
            raise TicketWriteError("blocked_by must not contain duplicates")
        if ticket_id in values:
            raise TicketWriteError("a ticket cannot block itself")
        for blocked_id in values:
            try:
                blocked = self.store.get(blocked_id)
            except KeyError as exc:
                raise TicketWriteError("blocked_by ticket not found") from exc
            pending = [blocked]
            seen: set[str] = set()
            while pending:
                current = pending.pop()
                if current.id in seen:
                    continue
                seen.add(current.id)
                if current.id == ticket_id:
                    raise TicketWriteError("blocked_by change would create a cycle")
                for dependency_id in current.blocked_by:
                    try:
                        pending.append(self.store.get(dependency_id))
                    except KeyError:
                        continue
        return values

    def create_ticket(self, data: dict[str, Any], *, actor: str) -> Ticket:
        if not isinstance(data, dict):
            raise TicketWriteError("request must be an object")
        self._validate_common(data)
        actor = _validate_actor(actor)
        process = data.get("process")
        ticket_type = data.get("type")
        if process not in TICKET_TYPES_BY_PROCESS:
            raise TicketWriteError("unknown process")
        if ticket_type not in TICKET_TYPES_BY_PROCESS[process]:
            raise TicketWriteError("invalid ticket type for process")
        title = _validate_title(data.get("title"))
        priority = _validate_priority(data.get("priority", 100))
        origin = _validate_origin(data.get("origin"))
        description = data.get("description", "")
        if not isinstance(description, str):
            raise TicketWriteError("description must be a string")
        parent_id = data.get("parent") or None
        if parent_id is not None and not isinstance(parent_id, str):
            raise TicketWriteError("parent must be a ticket ID or null")
        mandatory = data.get("mandatory", True)
        if not isinstance(mandatory, bool):
            raise TicketWriteError("mandatory must be boolean")
        status = data.get("status")
        workflow = load_workflow(process)
        if status is not None and status != workflow.initial_status:
            raise TicketWriteError("agent-created tickets must start at workflow initial status")
        with _WRITE_LOCK:
            key = data.get("idempotency_key")
            if key is not None and (not isinstance(key, str) or not key.strip()):
                raise TicketWriteError("idempotency_key must be a non-empty string")
            if key:
                for existing in self.store.list(process):
                    for event in existing.audit_events:
                        if event.get("operation") == "create_ticket" and event.get("idempotency_key") == key:
                            expected = event.get("after")
                            requested = {"process": process, "type": ticket_type, "title": title, "description": description, "priority": priority, "parent": parent_id, "mandatory": mandatory}
                            if expected == requested:
                                return existing
                            raise TicketWriteConflict("idempotency_key was already used with another payload")
            self._validate_parent(parent_id, process, ticket_type)
            ticket = self.store.create(process, ticket_type, title, description=description, priority=priority, parent=parent_id, mandatory=mandatory)
            ticket.audit_events.append({"event": "ticket_created", "timestamp": now_iso(), "actor": actor, "origin": origin, "operation": "create_ticket", "ticket_id": ticket.id, "changed_fields": sorted({"process", "type", "title", "description", "priority", "parent", "mandatory"}), "before": None, "after": {"process": process, "type": ticket_type, "title": title, "description": description, "priority": priority, "parent": parent_id, "mandatory": mandatory}, **({"idempotency_key": key} if key else {})})
            self.store.save(ticket)
            return ticket

    def update_ticket(self, ticket_id: str, data: dict[str, Any], *, actor: str) -> Ticket:
        if not isinstance(data, dict):
            raise TicketWriteError("request must be an object")
        self._validate_common(data, update=True)
        actor = _validate_actor(actor)
        origin = _validate_origin(data.get("origin"))
        with _WRITE_LOCK:
            try:
                ticket = self.store.get(ticket_id)
            except KeyError as exc:
                raise KeyError(ticket_id) from exc
            expected = data.get("expected_updated_at")
            if expected is not None and expected != ticket.updated_at:
                raise TicketWriteConflict("ticket was modified; expected_updated_at is stale")
            fields = set(data) - {"origin", "expected_updated_at"}
            if not fields:
                raise TicketWriteError("at least one metadata field is required")
            if "title" in data: data["title"] = _validate_title(data["title"])
            if "priority" in data: data["priority"] = _validate_priority(data["priority"])
            if "mandatory" in data and not isinstance(data["mandatory"], bool): raise TicketWriteError("mandatory must be boolean")
            if "description" in data and not isinstance(data["description"], str): raise TicketWriteError("description must be a string")
            if "context" in data and not isinstance(data["context"], dict): raise TicketWriteError("context must be an object")
            if "parent" in data: self._validate_parent(data["parent"], ticket.process, ticket.type, current_id=ticket.id)
            if "blocked_by" in data: data["blocked_by"] = self._validate_blocked_by(ticket.id, data["blocked_by"])
            before = _snapshot(ticket, fields)
            for field in fields: setattr(ticket, field, data[field])
            if "context" in fields:
                ticket.context_revision += 1
            after = _snapshot(ticket, fields)
            changed = sorted(field for field in fields if before[field] != after[field])
            if not changed:
                return ticket
            ticket.audit_events.append({"event": "ticket_updated", "timestamp": now_iso(), "actor": actor, "origin": origin, "operation": "update_ticket", "ticket_id": ticket.id, "changed_fields": changed, "before": {field: before[field] for field in changed}, "after": {field: after[field] for field in changed}})
            self.store.save(ticket)
            return ticket


def next_status_for_ticket(store: TicketStore, ticket: Ticket) -> str | None:
    workflow = load_workflow(ticket.process)
    stage = workflow.by_id[ticket.status]
    if ticket.process == "discovery" and ticket.type == "correction" and stage.id == "human":
        return "done"
    if ticket.process != "discovery" or stage.id != "investment_decision":
        return stage.next
    if ticket.implementation_required is not None:
        return "implementation" if ticket.implementation_required else "ready_for_validation"

    # Legacy tickets predate the explicit technical-analysis decision.
    linked = store.children_of(ticket.id, process="delivery")
    return "implementation" if linked else "ready_for_validation"


def automatic_retry_available(ticket: Ticket) -> bool:
    return ticket.last_outcome == "failed" and 0 <= ticket.consecutive_failures <= len(RETRY_BACKOFF_SECONDS)


def automatic_retry_due(ticket: Ticket, now: datetime | None = None) -> bool:
    if not automatic_retry_available(ticket):
        return False
    if ticket.retry_after is None:
        return True
    try:
        retry_after = datetime.fromisoformat(ticket.retry_after)
    except ValueError:
        return False
    if retry_after.tzinfo is None:
        retry_after = retry_after.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return retry_after <= current


def retry_exhausted(ticket: Ticket) -> bool:
    return ticket.last_outcome == "failed" and ticket.consecutive_failures > len(RETRY_BACKOFF_SECONDS)


def reset_failed_retry(ticket: Ticket) -> bool:
    workflow = load_workflow(ticket.process)
    stage = workflow.by_id.get(ticket.status)
    if not stage or stage.kind != "agent" or ticket.active_run or not retry_exhausted(ticket):
        return False
    ticket.consecutive_failures = 0
    ticket.retry_after = None
    return True
