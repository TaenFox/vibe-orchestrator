from pathlib import Path

import pytest

from vibe_orchestrator.agent_tools import AgentTicketTools, ReadOnlyAgentTools
from vibe_orchestrator.control import DeliverySessionStore
from vibe_orchestrator.tickets import TicketStore, TicketWriteConflict, TicketWriteError


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


def test_agent_write_contract_validates_lifecycle_and_audits(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    tools = AgentTicketTools(tmp_path, actor="agent-1")

    created = tools.create_ticket(process="delivery", type="task", title="  Safe task  ", origin="agent:run-1", idempotency_key="k1")
    assert created["title"] == "Safe task"
    assert created["status"] == "todo"
    assert created["audit_events"][-1]["origin"] == "agent:run-1"
    assert created["audit_events"][-1]["event"] == "ticket_created"

    replay = tools.create_ticket(process="delivery", type="task", title="Safe task", origin="agent:run-1", idempotency_key="k1")
    assert replay["id"] == created["id"]
    assert len(TicketStore(tmp_path).get(created["id"]).audit_events) == 1
    with pytest.raises(TicketWriteConflict):
        tools.create_ticket(process="delivery", type="task", title="Different", origin="agent:run-1", idempotency_key="k1")
    with pytest.raises(TicketWriteError):
        tools.create_ticket(process="delivery", type="task", title="Unsafe", origin="agent:run-1", status="review")
    with pytest.raises(TicketWriteError):
        tools.update_ticket(created["id"], status="review", origin="agent:run-1")

    updated = tools.update_ticket(created["id"], title="Renamed", context={"category": "tech-debt"}, origin="agent:run-1")
    assert updated["title"] == "Renamed"
    assert updated["status"] == "todo"
    assert updated["audit_events"][-1]["changed_fields"] == ["context", "title"]
    assert len(TicketStore(tmp_path).get(created["id"]).audit_events) == 2


def test_agent_write_rejects_invalid_metadata_without_changes(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    tools = AgentTicketTools(tmp_path, actor="agent-1")
    ticket = tools.create_ticket(process="delivery", type="task", title="Task", origin="agent")
    before = TicketStore(tmp_path).get(ticket["id"])
    with pytest.raises(TicketWriteError):
        tools.update_ticket(ticket["id"], priority=True, origin="agent")
    with pytest.raises(TicketWriteError):
        tools.update_ticket(ticket["id"], blocked_by=[ticket["id"]], origin="agent")
    after = TicketStore(tmp_path).get(ticket["id"])
    assert after.title == before.title
    assert len(after.audit_events) == 1
    with pytest.raises(TicketWriteConflict):
        tools.update_ticket(ticket["id"], title="stale", expected_updated_at="old", origin="agent")
    assert len(TicketStore(tmp_path).get(ticket["id"]).audit_events) == 1


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


def test_agent_ticket_source_artifacts_are_safe_encoded_links_and_read_only(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Source artifacts")
    run_id = "run with space"
    run_dir = tmp_path / ".vibe" / "runs" / run_id
    source_dir = run_dir / "source dir"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "файл name.txt"
    source_file.write_text("source", encoding="utf-8")
    ticket.run_history.append({
        "run_id": run_id,
        "source_artifact_path": ".vibe/runs/run with space/source dir",
    })
    store.save(ticket)
    before = (store.ticket_path(ticket)).read_bytes()

    payload = ReadOnlyAgentTools(tmp_path).get_ticket(ticket.id)
    source = payload["run_history"][-1]["source_artifacts"]

    assert source == {
        "path": ".vibe/runs/run with space/source dir",
        "links": ["/artifacts/run%20with%20space/source%20dir/%D1%84%D0%B0%D0%B9%D0%BB%20name.txt"],
    }
    assert (store.ticket_path(ticket)).read_bytes() == before


@pytest.mark.parametrize(
    ("artifact_relative_path", "expected_link"),
    [
        ("result.json", "/artifacts/run-1/result.json"),
        ("nested/result.json", "/artifacts/run-1/nested/result.json"),
    ],
)
def test_agent_ticket_file_artifacts_preserve_path_from_run_root(
    tmp_path: Path, artifact_relative_path: str, expected_link: str
):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "File artifact")
    artifact = tmp_path / ".vibe" / "runs" / "run-1" / artifact_relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("result", encoding="utf-8")
    artifact_path = f".vibe/runs/run-1/{artifact_relative_path}"
    ticket.run_history.append({"run_id": "run-1", "artifacts_path": artifact_path})
    store.save(ticket)

    entry = ReadOnlyAgentTools(tmp_path).get_ticket(ticket.id)["run_history"][-1]

    assert entry["artifacts"] == {"path": artifact_path, "links": [expected_link]}


def test_agent_ticket_artifact_from_another_run_is_ignored(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Cross-run artifact")
    artifact = tmp_path / ".vibe" / "runs" / "run-2" / "result.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("result", encoding="utf-8")
    artifact_path = ".vibe/runs/run-2/result.json"
    ticket.run_history.append({"run_id": "run-1", "artifacts_path": artifact_path})
    store.save(ticket)

    entry = ReadOnlyAgentTools(tmp_path).get_ticket(ticket.id)["run_history"][-1]

    assert entry["artifacts"] == {"path": artifact_path, "links": []}


def test_agent_ticket_source_artifact_precedence_and_invalid_paths(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Source precedence")
    run_dir = tmp_path / ".vibe" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "preferred.txt").write_text("preferred", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (run_dir / "escape.txt").symlink_to(outside)
    ticket.run_history.extend([
        {
            "run_id": "run-1",
            "source_artifacts": ".vibe/runs/run-1/preferred.txt",
            "source_artifact_path": ".vibe/runs/run-1/missing.txt",
        },
        {"run_id": "run-1", "source_artifact_path": str(outside)},
        {"run_id": "run-1", "source_artifact_path": ".vibe/runs/run-1/../outside.txt"},
        {"run_id": "run-1", "source_artifact_path": ".vibe/runs/run-1/escape.txt"},
        {"run_id": "run-1", "source_artifacts": {"path": str(outside)}},
    ])
    store.save(ticket)

    entries = ReadOnlyAgentTools(tmp_path).get_ticket(ticket.id)["run_history"][-5:]

    assert entries[0]["source_artifacts"]["path"].endswith("preferred.txt")
    assert entries[0]["source_artifacts"]["links"] == ["/artifacts/run-1/preferred.txt"]
    assert entries[1]["source_artifacts"]["links"] == []
    assert entries[2]["source_artifacts"]["links"] == []
    assert entries[3]["source_artifacts"]["links"] == []
    assert entries[4]["source_artifacts"] == {"path": {"path": str(outside)}, "links": []}


def test_agent_ticket_source_artifact_links_are_returned_by_list_tickets(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Listed source")
    run_dir = tmp_path / ".vibe" / "runs" / "run-2"
    run_dir.mkdir(parents=True)
    (run_dir / "source.txt").write_text("source", encoding="utf-8")
    ticket.run_history.append({"run_id": "run-2", "source_artifact_path": ".vibe/runs/run-2/source.txt"})
    store.save(ticket)

    result = ReadOnlyAgentTools(tmp_path).list_tickets(process="delivery")

    assert result["items"][0]["run_history"][-1]["source_artifacts"]["links"] == ["/artifacts/run-2/source.txt"]


@pytest.mark.parametrize("run_id", ["../outside", "/tmp/outside", "nested/run", "nested\\run", ".", ".."])
def test_agent_ticket_source_artifacts_reject_malicious_run_ids(tmp_path: Path, run_id: str):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Malicious run id")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    ticket.run_history.append({
        "run_id": run_id,
        "source_artifact_path": str(outside),
    })
    store.save(ticket)

    entry = ReadOnlyAgentTools(tmp_path).get_ticket(ticket.id)["run_history"][-1]

    assert entry["source_artifacts"] == {"path": str(outside), "links": []}


def test_agent_ticket_source_artifacts_reject_symlinked_run_directory(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Symlinked run directory")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    runs_root = tmp_path / ".vibe" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    (runs_root / "run-link").symlink_to(outside, target_is_directory=True)
    source_path = ".vibe/runs/run-link/secret.txt"
    ticket.run_history.append({"run_id": "run-link", "source_artifact_path": source_path})
    store.save(ticket)

    entry = ReadOnlyAgentTools(tmp_path).get_ticket(ticket.id)["run_history"][-1]

    assert entry["source_artifacts"] == {"path": source_path, "links": []}
