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
