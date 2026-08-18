from __future__ import annotations

import os
import logging
import tempfile
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
    context: dict[str, Any] = field(default_factory=dict)
    context_revision: int = 0
    consecutive_failures: int = 0
    retry_after: str | None = None
    run_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ticket":
        payload = dict(data)
        history = payload.get("run_history")
        if isinstance(history, list):
            payload["run_history"] = [dict(item) for item in history if isinstance(item, dict)]
        else:
            payload["run_history"] = []
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
