from pathlib import Path

from vibe_orchestrator.control_db_migration import migrate_control_plane
from vibe_orchestrator.sessions import SessionStore
from vibe_orchestrator.tickets import TicketStore


def test_ticket_store_uses_sqlite_after_migration(tmp_path: Path):
    yaml_store = TicketStore(tmp_path, use_database=False)
    yaml_store.init()
    ticket = yaml_store.create("delivery", "task", "Historical ticket")
    migrate_control_plane(tmp_path)

    store = TicketStore(tmp_path)
    loaded = store.get(ticket.id)
    loaded.title = "SQLite ticket"
    store.save(loaded)

    assert TicketStore(tmp_path).get(ticket.id).title == "SQLite ticket"
    assert TicketStore(tmp_path, use_database=False).get(ticket.id).title == "Historical ticket"


def test_session_store_uses_sqlite_after_migration(tmp_path: Path):
    yaml_tickets = TicketStore(tmp_path, use_database=False)
    yaml_tickets.init()
    ticket = yaml_tickets.create("delivery", "task", "Session member")
    yaml_sessions = SessionStore(tmp_path, yaml_tickets, use_database=False)
    session = yaml_sessions.create([ticket.id], title="Historical session")
    migrate_control_plane(tmp_path)

    tickets = TicketStore(tmp_path)
    sessions = SessionStore(tmp_path, tickets)
    loaded = sessions.get(session.id)
    loaded.title = "SQLite session"
    sessions.save(loaded)

    assert SessionStore(tmp_path, tickets).get(session.id).title == "SQLite session"
    assert SessionStore(tmp_path, TicketStore(tmp_path, use_database=False), use_database=False).get(session.id).title == "Historical session"
