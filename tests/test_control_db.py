import sqlite3
from pathlib import Path

from vibe_orchestrator.control_db import ControlPlaneReader, audit_control_plane
from vibe_orchestrator.control_db_migration import migrate_control_plane
from vibe_orchestrator.tickets import TicketStore


def test_reader_supports_filters_pagination_and_json_payloads(tmp_path: Path):
    store = TicketStore(tmp_path, use_database=False)
    store.init()
    first = store.create("delivery", "task", "First", status="todo", priority=20)
    store.create("discovery", "idea", "Second", status="ready", priority=10)
    migrate_control_plane(tmp_path)

    reader = ControlPlaneReader(tmp_path)
    page = reader.list_tickets(process="delivery", limit=1)

    assert [row["ticket_id"] for row in page] == [first.id]
    assert page[0]["payload"]["title"] == "First"
    assert reader.get_ticket(first.id)["ticket_id"] == first.id


def test_integrity_audit_reports_healthy_snapshot(tmp_path: Path):
    store = TicketStore(tmp_path, use_database=False)
    store.init()
    ticket = store.create("delivery", "task", "Audited")
    run_dir = tmp_path / ".vibe" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"run_id":"run-1","ticket_id":"%s","stage":"review"}' % ticket.id,
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text('{"outcome":"completed"}', encoding="utf-8")
    (run_dir / "events.jsonl").write_text('{"type":"thread.started"}\n', encoding="utf-8")
    migrate_control_plane(tmp_path)

    report = audit_control_plane(tmp_path)

    assert report.ok is True
    assert report.errors == ()


def test_integrity_audit_detects_broken_ticket_reference(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    run_dir = tmp_path / ".vibe" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"run_id":"run-1","ticket_id":"DEL-MISSING","stage":"review"}',
        encoding="utf-8",
    )
    migrate_control_plane(tmp_path)

    with sqlite3.connect(tmp_path / ".vibe" / "control.sqlite3") as db:
        db.execute("update runs set ticket_id = 'DEL-MISSING'")
        db.commit()

    report = audit_control_plane(tmp_path)

    assert report.ok is False
    assert any("missing ticket DEL-MISSING" in error for error in report.errors)
