from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import Workflow
from .tickets import Ticket


@dataclass(frozen=True)
class Candidate:
    ticket: Ticket
    source_status: str
    target_status: str
    stage_position: int


def _age_key(ticket: Ticket) -> datetime:
    return datetime.fromisoformat(ticket.created_at)


def wip_count(tickets: list[Ticket], status: str) -> int:
    return sum(1 for t in tickets if t.status == status and not t.wip_exempt)


def select_candidates(workflow: Workflow, tickets: list[Ticket], running_ids: set[str]) -> list[Candidate]:
    by_id = workflow.by_id
    candidates: list[Candidate] = []
    for ticket in tickets:
        if ticket.id in running_ids or ticket.active_run or ticket.blocked_by:
            continue
        source = by_id.get(ticket.status)
        if not source or source.kind != "queue" or not source.pull_to:
            continue
        target = by_id[source.pull_to]
        if target.kind != "agent":
            continue
        if not ticket.wip_exempt and target.wip is not None and wip_count(tickets, target.id) >= target.wip:
            continue
        candidates.append(Candidate(ticket=ticket, source_status=source.id, target_status=target.id, stage_position=workflow.position(source.id)))
    candidates.sort(key=lambda c: (-c.stage_position, 0 if c.ticket.wip_exempt else 1, c.ticket.priority, _age_key(c.ticket), c.ticket.id))
    return candidates
