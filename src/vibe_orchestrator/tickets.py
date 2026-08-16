from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import load_workflow


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
    blocked_by: list[str] = field(default_factory=list)
    mandatory: bool = True
    wip_exempt: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    active_run: str | None = None
    last_outcome: str | None = None
    last_summary: str | None = None
    run_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ticket":
        payload = dict(data)
        history = payload.get("run_history")
        if isinstance(history, list):
            payload["run_history"] = [dict(item) for item in history if isinstance(item, dict)]
        else:
            payload["run_history"] = []
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        if not payload["run_history"]:
            payload.pop("run_history")
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
                "Состояние тикетов для vibe-orchestrator. Коммитьте `tickets/`; `runs/` содержит локальные артефакты запусков (`run.json`, `events.jsonl`, `result.json`).\n",
                encoding="utf-8",
            )
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("runs/\n", encoding="utf-8")

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
        run_id: str,
        stage_id: str,
        event: str,
        **extra: Any,
    ) -> None:
        entry = {
            "run_id": run_id,
            "stage": stage_id,
            "event": event,
            "timestamp": now_iso(),
            "artifacts_path": f".vibe/runs/{run_id}",
        }
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

    def create(self, process: str, ticket_type: str, title: str, description: str = "", priority: int = 100, parent: str | None = None, status: str | None = None, wip_exempt: bool | None = None, mandatory: bool = True) -> Ticket:
        workflow = load_workflow(process)
        prefix = {"discovery": "DISC", "delivery": "DEL", "process_management": "PM"}[process]
        ticket_id = f"{prefix}-{uuid.uuid4().hex[:6].upper()}"
        if wip_exempt is None:
            wip_exempt = ticket_type in {"rework", "correction"}
        ticket = Ticket(id=ticket_id, process=process, type=ticket_type, title=title, status=status or workflow.initial_status, priority=priority, description=description, parent=parent, mandatory=mandatory, wip_exempt=wip_exempt)
        self.save(ticket)
        return ticket
