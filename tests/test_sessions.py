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


def test_save_cannot_bypass_persisted_lifecycle(tmp_path: Path):
    tickets, store = stores(tmp_path)
    ticket = tickets.create("delivery", "story", "Immutable membership")
    session = store.create([ticket.id])
    store.activate(session)

    session.status = "draft"
    session.ticket_ids.clear()
    with pytest.raises(ValueError, match="Invalid session status transition"):
        store.save(session)

    persisted = store.get(session.id)
    assert persisted.status == "active"
    assert persisted.ticket_ids == [ticket.id]

    session.status = "active"
    session.ticket_ids = [ticket.id]
    session.started_at = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="start time cannot be changed"):
        store.save(session)


def test_cancel_draft_has_consistent_timestamps(tmp_path: Path):
    _, store = stores(tmp_path)
    session = store.create()
    store.cancel(session)

    loaded = store.get(session.id)
    assert loaded.status == "cancelled"
    assert loaded.started_at is None
    assert loaded.cancelled_at


@pytest.mark.parametrize("field_name", ["started_at", "completed_at"])
def test_cancel_draft_rejects_lifecycle_timestamps(tmp_path: Path, field_name: str):
    _, store = stores(tmp_path)
    session = store.create()
    session.status = "cancelled"
    session.cancelled_at = "2020-01-01T00:00:00+00:00"
    setattr(session, field_name, "2020-01-01T00:00:00+00:00")

    with pytest.raises(ValueError, match="Cancelled draft sessions"):
        store.save(session)


def test_cancel_active_rejects_completed_timestamp(tmp_path: Path):
    tickets, store = stores(tmp_path)
    ticket = tickets.create("delivery", "story", "Active session")
    session = store.create([ticket.id])
    store.activate(session)
    session.status = "cancelled"
    session.cancelled_at = "2020-01-01T00:00:00+00:00"
    session.completed_at = "2020-01-01T00:00:00+00:00"

    with pytest.raises(ValueError, match="Cancelled active sessions"):
        store.save(session)


@pytest.mark.parametrize(
    ("status", "terminal_fields", "message"),
    [
        (
            "completed",
            {
                "started_at": "2020-01-01T00:00:00+00:00",
                "completed_at": "2020-01-01T00:00:00+00:00",
                "cancelled_at": "2020-01-01T00:00:00+00:00",
            },
            "Completed sessions",
        ),
        (
            "cancelled",
            {
                "cancelled_at": "2020-01-01T00:00:00+00:00",
                "completed_at": "2020-01-01T00:00:00+00:00",
            },
            "Cancelled sessions",
        ),
    ],
)
def test_save_rejects_incompatible_terminal_timestamps_for_new_sessions(
    tmp_path: Path,
    status: str,
    terminal_fields: dict[str, str],
    message: str,
):
    _, store = stores(tmp_path)
    session = DeliverySession(id=f"SESSION-{status.upper()}", status=status, **terminal_fields)

    with pytest.raises(ValueError, match=message):
        store.save(session)


@pytest.mark.parametrize(
    ("status", "fields", "message"),
    [
        ("active", {"ticket_ids": []}, "cannot be empty"),
        ("active", {}, "started_at"),
        ("completed", {}, "started_at"),
        ("completed", {"started_at": "2020-01-01T00:00:00+00:00"}, "completed_at"),
        ("cancelled", {}, "cancelled_at"),
    ],
)
def test_save_rejects_invalid_new_lifecycle(
    tmp_path: Path, status: str, fields: dict[str, object], message: str
):
    tickets, store = stores(tmp_path)
    session = DeliverySession(id=f"SESSION-{status.upper()}", status=status, **fields)
    if status == "active" and message == "started_at":
        ticket = tickets.create("delivery", "story", "Active session")
        session.ticket_ids = [ticket.id]

    with pytest.raises(ValueError, match=message):
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
