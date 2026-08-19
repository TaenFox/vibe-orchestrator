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


def test_completed_session_keeps_completed_ticket_history(tmp_path: Path):
    tickets, store = stores(tmp_path)
    ticket = tickets.create("delivery", "story", "Completed work")
    session = store.create([ticket.id])
    store.activate(session)
    ticket.status = "done"
    tickets.save(ticket)
    store.complete(session)

    loaded = store.get(session.id)
    assert loaded.status == "completed"
    assert loaded.ticket_ids == [ticket.id]


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


def test_rework_inherits_active_session_and_is_idempotent(tmp_path: Path):
    tickets, store = stores(tmp_path)
    parent = tickets.create("delivery", "task", "Parent", status="review")
    rework = tickets.create("delivery", "rework", "Rework", parent=parent.id, status="selected_for_session")
    session = store.create([parent.id])
    store.activate(session)

    store.inherit_ticket(session, rework.id, source_ticket=parent.id)
    store.inherit_ticket(session, rework.id, source_ticket=parent.id)

    loaded = store.get(session.id)
    assert loaded.ticket_ids == [parent.id, rework.id]


def test_active_session_can_inherit_rework_after_parent_completed(tmp_path: Path):
    tickets = TicketStore(tmp_path)
    parent = tickets.create("delivery", "task", "Parent", status="todo")
    session = DeliverySessionStore(tmp_path).store.create([parent.id])
    sessions = DeliverySessionStore(tmp_path)
    sessions.activate(session.id, tickets)
    parent.status = "done"
    tickets.save(parent)
    rework = tickets.create("delivery", "rework", "Rework", parent=parent.id, status="selected_for_session")

    sessions.store.inherit_ticket(sessions.get(session.id), rework.id, source_ticket=parent.id)

    assert rework.id in sessions.store.effective_ticket_ids(sessions.get(session.id))


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


def test_invalid_type_duplicate_and_legacy_missing_updated_at(tmp_path: Path):
    tickets, store = stores(tmp_path)
    store = SessionStore(tmp_path, tickets, use_database=False)
    discovery_ticket = tickets.create("discovery", "idea", "Wrong process")
    session = store.create()
    with pytest.raises(ValueError, match="Invalid delivery ticket type"):
        store.add_ticket(session, discovery_ticket.id)
    with pytest.raises(TypeError):
        store.add_ticket(session, 42)  # type: ignore[arg-type]

    legacy = tmp_path / ".vibe" / "sessions" / "SESSION-LEGACY.yaml"
    legacy.write_text(yaml.safe_dump({"id": "SESSION-LEGACY", "status": "draft", "ticket_ids": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="updated_at"):
        store.get("SESSION-LEGACY")


def test_legacy_updated_at_is_restored_from_created_at(tmp_path: Path):
    tickets, _ = stores(tmp_path)
    store = SessionStore(tmp_path, tickets, use_database=False)
    path = tmp_path / ".vibe" / "sessions" / "SESSION-LEGACY.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "id": "SESSION-LEGACY",
                "status": "draft",
                "ticket_ids": [],
                "created_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    session = store.get("SESSION-LEGACY")

    assert session.updated_at == session.created_at
    store.save(session)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["updated_at"]


def test_legacy_aggregate_store_is_migrated_to_versioned_session_files(tmp_path: Path):
    tickets, _ = stores(tmp_path)
    store = SessionStore(tmp_path, tickets, use_database=False)
    legacy_root = tmp_path / ".vibe" / "tmp"
    legacy_root.mkdir(parents=True)
    legacy_root.joinpath("delivery-sessions.yaml").write_text(
        yaml.safe_dump(
            {
                "sessions": [
                    {
                        "id": "SES-ABC123",
                        "title": "Release",
                        "status": "draft",
                        "participants": [],
                        "created_at": "2020-01-01T00:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    migrated = store.list()

    assert [session.id for session in migrated] == ["SESSION-ABC123"]
    assert migrated[0].title == "Release"
    assert (tmp_path / ".vibe" / "sessions" / "SESSION-ABC123.yaml").exists()


@pytest.mark.parametrize("updated_at", [None, "not-a-date"])
def test_load_rejects_missing_or_invalid_updated_at(tmp_path: Path, updated_at: object):
    _, store = stores(tmp_path)
    payload = {
        "id": "SESSION-CORRUPT",
        "status": "draft",
        "ticket_ids": [],
        "created_at": "2020-01-01T00:00:00+00:00",
    }
    if updated_at is not None:
        payload["updated_at"] = updated_at
    else:
        payload["updated_at"] = None
    path = tmp_path / ".vibe" / "sessions" / "SESSION-CORRUPT.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="updated_at|ISO-8601"):
        store.load_path(path)


@pytest.mark.parametrize("updated_at", [None, "not-a-date"])
def test_save_rejects_missing_or_invalid_updated_at(tmp_path: Path, updated_at: object):
    _, store = stores(tmp_path)
    session = DeliverySession(id="SESSION-CORRUPT", updated_at=updated_at)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="updated_at|ISO-8601"):
        store.save(session)


def test_load_rejects_updated_at_before_lifecycle_timestamps(tmp_path: Path):
    _, store = stores(tmp_path)
    path = tmp_path / ".vibe" / "sessions" / "SESSION-STALE.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "id": "SESSION-STALE",
                "status": "active",
                "ticket_ids": ["DEL-FAKE"],
                "created_at": "2020-01-01T00:00:00+00:00",
                "updated_at": "2020-01-01T00:00:00+00:00",
                "started_at": "2020-01-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="updated_at cannot precede started_at"):
        store.load_path(path)


@pytest.mark.parametrize(
    "session_id", ["", "SESSION-", "../outside", "SESSION-abc", "SESSION-A/B", "SESSION-A\\B"]
)
def test_session_ids_are_safe_tokens(tmp_path: Path, session_id: str):
    _, store = stores(tmp_path)
    session = DeliverySession(id=session_id)

    with pytest.raises(ValueError, match="Session ID"):
        store.session_path(session_id)
    with pytest.raises(ValueError, match="Session ID"):
        store.save(session)
    with pytest.raises(ValueError, match="Session ID"):
        store.get(session_id)


def test_session_paths_cannot_escape_sessions_directory(tmp_path: Path):
    _, store = stores(tmp_path)
    outside = tmp_path / ".vibe" / "outside.yaml"
    outside.write_text("id: SESSION-OUTSIDE\nstatus: draft\nticket_ids: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="inside .vibe/sessions"):
        store.load_path(outside)

    malicious = DeliverySession(id="SESSION-VALID")
    malicious.id = "../outside"
    with pytest.raises(ValueError, match="Session ID"):
        store.save(malicious)
    assert outside.read_text(encoding="utf-8").startswith("id: SESSION-OUTSIDE")

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
    assert not (tmp_path / ".vibe/tmp/delivery-session.yaml").exists()

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


def test_session_activation_does_not_partially_select_tickets_on_invalid_membership(tmp_path: Path):
    tickets = TicketStore(tmp_path)
    tickets.init()
    first = tickets.create("delivery", "task", "First")
    sessions = DeliverySessionStore(tmp_path)
    session = sessions.create("Release")
    session.participants = [first.id, "DEL-MISSING"]
    with pytest.raises(SessionError, match="Unknown ticket"):
        sessions._replace(session)

    assert tickets.get(first.id).status == "todo"
    assert sessions.get(session.id).status == "draft"
    assert not (tmp_path / ".vibe/tmp/delivery-session.yaml").exists()


def test_session_parser_keeps_existing_ticket_commands_and_supports_alias():
    from vibe_orchestrator.cli import build_parser

    assert build_parser().parse_args(["add", "/tmp", "delivery", "task", "Ticket"]).command == "add"
    args = build_parser().parse_args(["sessions", "complete", "/tmp", "SES-1", "--reason", "manual"])
    assert args.command == "sessions"
    assert args.session_command == "complete"
    assert args.override_reason == "manual"
