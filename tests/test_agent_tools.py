from pathlib import Path

import pytest

from vibe_orchestrator.agent_tools import ReadOnlyAgentTools
from vibe_orchestrator.control import DeliverySessionStore
from vibe_orchestrator.tickets import TicketStore


def test_agent_ticket_queries_are_filtered_bounded_and_read_only(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    parent = store.create("discovery", "idea", "Parent")
    member = store.create("delivery", "task", "Member", parent=parent.id, status="review", description="full description")
    store.create("delivery", "task", "Other", status="todo")
    session = DeliverySessionStore(tmp_path).create("Release")
    DeliverySessionStore(tmp_path).add(session.id, member.id, store)

    tools = ReadOnlyAgentTools(tmp_path)
    result = tools.list_tickets(process="delivery", status="review", parent=parent.id, session=session.id, limit=1)

    assert result["contract_version"] == "agent.read.v1"
    assert [item["id"] for item in result["items"]] == [member.id]
    assert result["items"][0]["description"] == "full description"
    assert not (tmp_path / ".vibe" / "sessions").glob("*.yaml") or (tmp_path / ".vibe" / "sessions" / f"{session.id}.yaml").exists()

    with pytest.raises(ValueError):
        tools.list_tickets(limit=0)


def test_agent_session_exposes_audit_and_effective_membership(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Member")
    sessions = DeliverySessionStore(tmp_path)
    session = sessions.create("Release")
    sessions.add(session.id, ticket.id, store)
    session = sessions.get(session.id)
    # The override is an audit event and therefore part of effective membership.
    session.status = "active"
    session.started_at = session.created_at
    store_session = sessions.store
    store_session.save(session)
    store_session.override_ticket(session, "DEL-EXTRA", actor="agent", reason="approved")

    payload = ReadOnlyAgentTools(tmp_path).get_session(session.id)

    assert payload["participants"] == [ticket.id]
    assert set(payload["effective_membership"]) == {"DEL-EXTRA", ticket.id}
    assert payload["audit_events"][-1]["event"] == "membership_override"
