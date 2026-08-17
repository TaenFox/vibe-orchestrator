from pathlib import Path

import pytest
import yaml

from vibe_orchestrator.sessions import DeliverySession, SessionStore
from vibe_orchestrator.tickets import TicketStore


def stores(tmp_path: Path) -> tuple[TicketStore, SessionStore]:
    tickets = TicketStore(tmp_path)
    tickets.init()
    return tickets, SessionStore(tmp_path)


def test_session_lifecycle_reload_and_audit(tmp_path: Path):
    tickets, store = stores(tmp_path)
    ticket = tickets.create("delivery", "story", "Persistent session")
    session = store.create([ticket.id])

    store.activate(session)
    store.complete(session)
    loaded = store.get(session.id)

    assert loaded.status == "completed"
    assert loaded.ticket_ids == [ticket.id]
    assert loaded.schema_version == 1
    assert loaded.started_at and loaded.completed_at
    assert [event["event"] for event in loaded.audit_events] == ["created", "activated", "completed"]


def test_membership_invariants_and_one_open_session(tmp_path: Path):
    tickets, store = stores(tmp_path)
    ticket = tickets.create("delivery", "task", "Only once")
    session = store.create()
    store.add_ticket(session, ticket.id)

    with pytest.raises(ValueError, match="already belongs"):
        store.add_ticket(session, ticket.id)
    with pytest.raises(ValueError, match="already belongs"):
        store.create([ticket.id])

    store.activate(session)
    with pytest.raises(ValueError, match="only be changed in draft"):
        store.remove_ticket(session, ticket.id)
    session.ticket_ids.clear()
    with pytest.raises(ValueError, match="only be changed in draft"):
        store.save(session)


def test_invalid_type_duplicate_and_legacy_reload(tmp_path: Path):
    tickets, store = stores(tmp_path)
    discovery_ticket = tickets.create("discovery", "idea", "Wrong process")
    session = store.create()
    with pytest.raises(ValueError, match="Invalid delivery ticket type"):
        store.add_ticket(session, discovery_ticket.id)
    with pytest.raises(TypeError):
        store.add_ticket(session, 42)  # type: ignore[arg-type]

    legacy = tmp_path / ".vibe" / "sessions" / "SESSION-LEGACY.yaml"
    legacy.write_text(yaml.safe_dump({"id": "SESSION-LEGACY", "status": "draft", "ticket_ids": []}), encoding="utf-8")
    loaded = store.get("SESSION-LEGACY")
    assert loaded.schema_version == 1
    assert loaded.audit_events == []
    assert loaded.created_at == loaded.updated_at
