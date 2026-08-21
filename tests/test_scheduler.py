from datetime import datetime, timedelta, timezone

from vibe_orchestrator.config import load_workflow
from vibe_orchestrator.scheduler import select_candidates
from vibe_orchestrator.tickets import Ticket


def ticket(id,status,*,priority=100,wip_exempt=False,created="2026-01-01T00:00:00+00:00"):
    return Ticket(id=id,process="delivery",type="task",title=id,status=status,priority=priority,wip_exempt=wip_exempt,created_at=created)


def test_rightmost_queue_wins():
    workflow=load_workflow("delivery"); tickets=[ticket("A","selected_for_session"),ticket("B","ready_for_review")]; assert select_candidates(workflow,tickets,set())[0].ticket.id=="B"


def test_wip_blocks_normal_ticket():
    workflow=load_workflow("delivery"); tickets=[ticket("A","ready_for_review"),ticket("R1","review"),ticket("R2","review")]; candidates=select_candidates(workflow,tickets,set()); assert all(c.ticket.id!="A" for c in candidates)


def test_rework_is_wip_exempt_and_preferred():
    workflow=load_workflow("delivery"); tickets=[ticket("NORMAL","selected_for_session",priority=10),ticket("REWORK","selected_for_session",priority=100,wip_exempt=True),ticket("D1","system_analysis"),ticket("D2","system_analysis"),ticket("D3","system_analysis")]; assert select_candidates(workflow,tickets,set())[0].ticket.id=="REWORK"


def test_parent_retries_same_agent_stage_after_rework():
    workflow=load_workflow("delivery"); parent=ticket("PARENT","review"); parent.last_outcome="needs_rework"; assert select_candidates(workflow,[parent],set())[0].target_status=="review"


def test_blocked_ticket_is_not_scheduled():
    workflow = load_workflow("delivery")
    blocked = ticket("BLOCKED", "selected_for_session")
    blocked.blocked_reason = "rework_cycle_stopped"

    assert select_candidates(workflow, [blocked], set()) == []


def test_parent_retries_same_agent_stage_after_correction():
    workflow=load_workflow("discovery"); parent=Ticket(id="DISC-1",process="discovery",type="idea",title="Idea",status="analysis",last_outcome="needs_correction",created_at="2026-01-01T00:00:00+00:00"); assert select_candidates(workflow,[parent],set())[0].target_status=="analysis"


def test_failed_agent_retries_only_after_backoff():
    workflow = load_workflow("delivery")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    failed = ticket("FAILED", "review")
    failed.last_outcome = "failed"
    failed.consecutive_failures = 1
    failed.retry_after = (now + timedelta(seconds=5)).isoformat()

    assert select_candidates(workflow, [failed], set(), now=now) == []
    assert select_candidates(workflow, [failed], set(), now=now + timedelta(seconds=5))[0].target_status == "review"


def test_failed_agent_stops_after_retry_budget_is_exhausted():
    workflow = load_workflow("delivery")
    failed = ticket("FAILED", "review")
    failed.last_outcome = "failed"
    failed.consecutive_failures = 3

    assert select_candidates(workflow, [failed], set()) == []


def test_active_delivery_session_allows_only_participants_into_system_analysis():
    workflow = load_workflow("delivery")
    participant = ticket("PARTICIPANT", "selected_for_session")
    outside = ticket("OUTSIDE", "selected_for_session")

    candidates = select_candidates(workflow, [participant, outside], set(), session_participants={"PARTICIPANT"})

    assert [candidate.ticket.id for candidate in candidates] == ["PARTICIPANT"]


def test_no_active_session_keeps_legacy_selected_tickets_schedulable():
    workflow = load_workflow("delivery")
    legacy = ticket("LEGACY", "selected_for_session")

    assert select_candidates(workflow, [legacy], set())[0].ticket.id == "LEGACY"


def test_no_active_session_excludes_deferred_technical_debt():
    workflow = load_workflow("delivery")
    debt = ticket("DEBT", "selected_for_session")
    debt.technical_debt_deferred = True

    assert select_candidates(workflow, [debt], set()) == []


def test_deferred_technical_debt_requires_active_session_membership():
    workflow = load_workflow("delivery")
    debt = ticket("DEBT", "selected_for_session")
    debt.technical_debt_deferred = True

    assert select_candidates(workflow, [debt], set(), session_participants=set()) == []
    assert [candidate.ticket.id for candidate in select_candidates(workflow, [debt], set(), session_participants={"DEBT"})] == ["DEBT"]


def test_legacy_ticket_without_marker_loads_schedulable():
    workflow = load_workflow("delivery")
    legacy = Ticket.from_dict({"id": "LEGACY", "process": "delivery", "type": "task", "title": "Legacy", "status": "selected_for_session"})

    assert legacy.technical_debt_deferred is False
    assert select_candidates(workflow, [legacy], set())[0].ticket.id == "LEGACY"


def test_active_legacy_session_blocks_external_rework_even_when_wip_exempt():
    workflow = load_workflow("delivery")
    rework = ticket("REWORK", "selected_for_session", wip_exempt=True)

    assert select_candidates(workflow, [rework], set(), session_participants=set()) == []


def test_active_legacy_session_allows_member_wip_exempt_rework():
    workflow = load_workflow("delivery")
    rework = ticket("REWORK", "selected_for_session", wip_exempt=True)

    assert [c.ticket.id for c in select_candidates(workflow, [rework], set(), session_participants={"REWORK"})] == ["REWORK"]
