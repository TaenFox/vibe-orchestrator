from pathlib import Path

import pytest
import yaml

from vibe_orchestrator.control import DeliverySessionStore, SessionError
from vibe_orchestrator.tickets import TicketStore


def test_session_lifecycle_updates_scheduler_compatibility_file(tmp_path: Path):
    tickets = TicketStore(tmp_path)
    tickets.init()
    first = tickets.create("delivery", "story", "First")
    second = tickets.create("delivery", "task", "Second")
    sessions = DeliverySessionStore(tmp_path)
    session = sessions.create("Release")

    sessions.add(session.id, first.id, tickets)
    sessions.add(session.id, second.id, tickets)
    active = sessions.activate(session.id, tickets)

    assert active.status == "active"
    assert tickets.get(first.id).status == "selected_for_session"
    assert yaml.safe_load((tmp_path / ".vibe/tmp/delivery-session.yaml").read_text()) == {
        "active": True,
        "session_id": session.id,
        "participants": [first.id, second.id],
    }

    with pytest.raises(SessionError, match="неполная"):
        sessions.complete(session.id, tickets)

    completed = sessions.complete(session.id, tickets, "Первый тикет еще не нужен")
    assert completed.status == "completed"
    assert not (tmp_path / ".vibe/tmp/delivery-session.yaml").exists()


def test_session_rejects_invalid_membership_and_lifecycle(tmp_path: Path):
    tickets = TicketStore(tmp_path)
    tickets.init()
    discovery = tickets.create("discovery", "idea", "Not delivery")
    done = tickets.create("delivery", "task", "Done", status="done")
    sessions = DeliverySessionStore(tmp_path)
    session = sessions.create("Release")

    with pytest.raises(SessionError, match="только Delivery"):
        sessions.add(session.id, discovery.id, tickets)
    with pytest.raises(SessionError, match="Завершенный"):
        sessions.add(session.id, done.id, tickets)
    with pytest.raises(SessionError, match="пустую"):
        sessions.activate(session.id, tickets)
    with pytest.raises(SessionError, match="только активную"):
        sessions.cancel(session.id, tickets)


def test_session_parser_keeps_existing_ticket_commands_and_supports_alias():
    from vibe_orchestrator.cli import build_parser

    assert build_parser().parse_args(["add", "/tmp", "delivery", "task", "Ticket"]).command == "add"
    args = build_parser().parse_args(["sessions", "complete", "/tmp", "SES-1", "--reason", "manual"])
    assert args.command == "sessions"
    assert args.session_command == "complete"
    assert args.override_reason == "manual"
