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


def select_candidates(
    workflow: Workflow,
    tickets: list[Ticket],
    running_ids: set[str],
    now: datetime | None = None,
    session_participants: set[str] | None = None,
) -> list[Candidate]:
    by_id = workflow.by_id
    candidates: list[Candidate] = []
    # WIP is a property of the input snapshot.  Computing it inside the
    # candidate loop made this polling path quadratic for large boards.
    wip_statuses = {stage.id for stage in by_id.values() if stage.wip is not None}
    wip_counts = {status: 0 for status in wip_statuses}
    for ticket in tickets:
        if ticket.status in wip_counts and not ticket.wip_exempt:
            wip_counts[ticket.status] += 1
    for ticket in tickets:
        if ticket.id in running_ids or ticket.active_run or ticket.blocked_by or ticket.blocked_reason:
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
        if source.kind == "queue" and not ticket.wip_exempt and target.wip is not None and wip_counts.get(target.id, 0) >= target.wip:
            continue
        candidates.append(Candidate(ticket=ticket, source_status=source.id, target_status=target.id, stage_position=workflow.position(source.id)))
    candidates.sort(key=lambda c: (-c.stage_position, 0 if c.ticket.wip_exempt else 1, c.ticket.priority, _age_key(c.ticket), c.ticket.id))
    return candidates
