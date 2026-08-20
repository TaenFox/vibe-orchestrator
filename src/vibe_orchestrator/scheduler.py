from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import Workflow
from .tickets import Ticket, automatic_retry_due


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


def _is_done(ticket: Ticket, workflow: Workflow) -> bool:
    stage = workflow.by_id.get(ticket.status)
    return bool(stage and stage.kind == "done")


def _is_ancestor(candidate_id: str, ticket: Ticket, by_id: dict[str, Ticket]) -> bool:
    current = ticket.parent
    visited: set[str] = set()
    while current and current not in visited:
        if current == candidate_id:
            return True
        visited.add(current)
        parent = by_id.get(current)
        current = parent.parent if parent else None
    return False


def effective_blockers(ticket: Ticket, tickets: list[Ticket], workflow: Workflow) -> set[str]:
    """Resolve inherited parent gates without creating self-blocking branches."""
    by_id = {item.id: item for item in tickets}
    blockers = set(ticket.blocked_by)
    current = ticket.parent
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        parent = by_id.get(current)
        if parent is None:
            break
        parent_blockers = set(parent.blocked_by)
        # A direct resolver must remain runnable. Its descendants also skip
        # their own ancestor blocker, while inheriting sibling gates.
        if ticket.id not in parent_blockers:
            blockers.update(
                blocker_id
                for blocker_id in parent_blockers
                if blocker_id != ticket.id and not _is_ancestor(blocker_id, ticket, by_id)
            )
        current = parent.parent
    return {
        blocker_id
        for blocker_id in blockers
        if (blocker := by_id.get(blocker_id)) is not None and not _is_done(blocker, workflow)
    }


def select_candidates(
    workflow: Workflow,
    tickets: list[Ticket],
    running_ids: set[str],
    now: datetime | None = None,
    session_participants: set[str] | None = None,
) -> list[Candidate]:
    by_id = workflow.by_id
    effective_by_id = {ticket.id: effective_blockers(ticket, tickets, workflow) for ticket in tickets}
    candidates: list[Candidate] = []
    for ticket in tickets:
        if ticket.id in running_ids or ticket.active_run or effective_by_id[ticket.id] or ticket.blocked_reason:
            continue
        # Technical-debt children are deferred until explicitly included in
        # an active Delivery session. Preserve legacy scheduling for ordinary
        # tickets when session_participants is None.
        if ticket.technical_debt_deferred and session_participants is None:
            continue
        source = by_id.get(ticket.status)
        if not source:
            continue
        if source.kind == "queue" and source.pull_to:
            target = by_id[source.pull_to]
        elif source.kind == "agent" and automatic_retry_due(ticket, now):
            target = source
        elif source.kind == "agent" and ticket.last_outcome and (source.outcomes or {}).get(ticket.last_outcome) == source.id:
            target = source
        else:
            continue
        if target.kind != "agent":
            continue
        if (
            session_participants is not None
            and source.id == "selected_for_session"
            and target.id == "system_analysis"
            and ticket.id not in session_participants
        ):
            continue
        if source.kind == "queue" and not ticket.wip_exempt and target.wip is not None and wip_count(tickets, target.id) >= target.wip:
            continue
        candidates.append(Candidate(ticket=ticket, source_status=source.id, target_status=target.id, stage_position=workflow.position(source.id)))
    candidates.sort(key=lambda c: (-c.stage_position, 0 if c.ticket.wip_exempt else 1, c.ticket.priority, _age_key(c.ticket), c.ticket.id))
    return candidates
