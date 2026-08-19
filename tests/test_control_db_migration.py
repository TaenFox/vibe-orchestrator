import json
from pathlib import Path

from vibe_orchestrator.control_db_migration import migrate_control_plane
from vibe_orchestrator.sessions import SessionStore
from vibe_orchestrator.tickets import TicketStore


def test_migration_imports_control_plane_and_is_repeatable(tmp_path: Path):
    tickets = TicketStore(tmp_path)
    tickets.init()
    ticket = tickets.create("delivery", "task", "Imported ticket", status="todo")
    session_store = SessionStore(tmp_path, tickets)
    session = session_store.create([ticket.id], title="Imported session")
    run_dir = tmp_path / ".vibe" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": "run-1", "ticket_id": ticket.id, "stage": "system_analysis"}), encoding="utf-8")
    (run_dir / "prompt.contract.txt").write_text("Applied prompt", encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps({
        "outcome": "completed",
        "summary": "Imported result",
        "token_usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }), encoding="utf-8")
    (run_dir / "events.jsonl").write_text('{"type":"thread.started"}\n{"type":"turn.completed"}\n', encoding="utf-8")

    database = tmp_path / ".vibe" / "control.sqlite3"
    first = migrate_control_plane(tmp_path, database)
    second = migrate_control_plane(tmp_path, database)

    assert first == second
    assert first.tickets == 1
    assert first.sessions == 1
    assert first.runs == 1
    assert first.prompts == 1
    assert first.results == 1
    assert first.run_events == 2
    assert first.telemetry == 1
    assert first.events >= 2

    import sqlite3
    with sqlite3.connect(database) as db:
        assert db.execute("select count(*) from tickets").fetchone()[0] == 1
        assert db.execute("select count(*) from session_members").fetchone()[0] == 1
        assert db.execute("select count(*) from runs").fetchone()[0] == 1
        assert db.execute("select prompt_text from prompt_contracts").fetchone()[0] == "Applied prompt"
        assert db.execute("select prompt_hash from runs").fetchone()[0]
        assert db.execute("select summary from run_results").fetchone()[0] == "Imported result"
        assert db.execute("select count(*) from run_events").fetchone()[0] == 2
        assert db.execute("select total_tokens from token_usage").fetchone()[0] == 14


def test_migration_dry_run_does_not_create_database(tmp_path: Path):
    tickets = TicketStore(tmp_path)
    tickets.init()
    tickets.create("discovery", "idea", "Dry run")
    database = tmp_path / ".vibe" / "control.sqlite3"

    report = migrate_control_plane(tmp_path, database, dry_run=True)

    assert report.dry_run is True
    assert not database.exists()


def test_migration_preserves_effective_session_membership(tmp_path: Path):
    tickets = TicketStore(tmp_path)
    tickets.init()
    direct = tickets.create("delivery", "task", "Direct member", status="todo")
    inherited = tickets.create("delivery", "task", "Effective member", status="todo")
    session_store = SessionStore(tmp_path, tickets)
    session = session_store.create([direct.id], title="Membership session")
    session_store.activate(session)
    session_store.override_ticket(session, inherited.id, actor="test", reason="preserve effective membership")

    database = tmp_path / ".vibe" / "control.sqlite3"
    migrate_control_plane(tmp_path, database)

    import sqlite3
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "select ticket_id, membership_kind from session_members order by position"
        ).fetchall()

    assert rows == [(direct.id, "direct"), (inherited.id, "effective")]
