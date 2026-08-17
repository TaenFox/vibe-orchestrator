from pathlib import Path

import yaml

from vibe_orchestrator.tickets import TicketStore


def test_create_and_reload(tmp_path: Path):
    store=TicketStore(tmp_path); store.init(); created=store.create("discovery","idea","Test idea",description="Hello"); loaded=store.get(created.id); assert loaded.title=="Test idea"; assert loaded.status=="todo"; assert loaded.description=="Hello"


def test_rework_defaults_to_wip_exempt(tmp_path: Path):
    store=TicketStore(tmp_path); store.init(); ticket=store.create("delivery","rework","Fix review",parent="DEL-ABC"); assert ticket.wip_exempt is True


def test_correction_defaults_to_wip_exempt(tmp_path: Path):
    store=TicketStore(tmp_path); store.init(); ticket=store.create("discovery","correction","Clarify discovery",parent="DISC-ABC"); assert ticket.wip_exempt is True


def test_delivery_child_mandatory_flag_is_persisted(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()

    ticket = store.create("delivery", "task", "Optional follow-up", parent="DISC-ABC", mandatory=False)

    assert store.get(ticket.id).mandatory is False


def test_discovery_implementation_decision_is_persisted(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("discovery", "idea", "No implementation")
    ticket.implementation_required = False
    store.save(ticket)

    assert store.get(ticket.id).implementation_required is False


def test_loads_legacy_ticket_without_run_history(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    path = tmp_path / ".vibe" / "tickets" / "delivery" / "DEL-LEGACY.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "DEL-LEGACY",
                "process": "delivery",
                "type": "story",
                "title": "Legacy ticket",
                "status": "review",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "last_outcome": "needs_rework",
                "last_summary": "Открыт до истории запусков",
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    ticket = store.get("DEL-LEGACY")

    assert ticket.run_history == []
    assert ticket.last_outcome == "needs_rework"
    assert ticket.last_summary == "Открыт до истории запусков"
    assert ticket.mandatory is True


def test_save_persists_run_history_with_artifact_path(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "story", "Track runs")
    store.record_run_event(ticket, run_id="run-123", stage_id="review", event="started", from_status="ready_for_review", prompt_path="delivery/review.md", prompt_version="sha256:abc", model="gpt-5.4", reasoning_effort="high", ticket_title="Track runs", ticket_priority="100", ticket_parent="none", ticket_description="(пусто)")
    store.record_run_event(ticket, run_id="run-123", stage_id="review", event="completed", outcome="completed", summary="Проверка пройдена", prompt_path="delivery/review.md", prompt_version="sha256:abc", model="gpt-5.4", reasoning_effort="high", ticket_title="Track runs", ticket_priority="100", ticket_parent="none", ticket_description="(пусто)")
    store.save(ticket)

    reloaded = store.get(ticket.id)

    assert [entry["event"] for entry in reloaded.run_history] == ["started", "completed"]
    assert all(entry["artifacts_path"] == ".vibe/runs/run-123" for entry in reloaded.run_history)
    assert all(entry["prompt_path"] == "delivery/review.md" for entry in reloaded.run_history)
    assert all(entry["prompt_version"] == "sha256:abc" for entry in reloaded.run_history)
    assert all(entry["ticket_title"] == "Track runs" for entry in reloaded.run_history)
    assert all(entry["ticket_priority"] == "100" for entry in reloaded.run_history)
    assert reloaded.to_dict()["run_history"][1]["summary"] == "Проверка пройдена"


def test_children_of_returns_creation_order_instead_of_ticket_id_order(tmp_path: Path):
    store=TicketStore(tmp_path); store.init(); parent=store.create("discovery","idea","Parent")
    later=store.create("delivery","task","Later child",parent=parent.id)
    earlier=store.create("delivery","story","Earlier child",parent=parent.id)
    later.created_at="2026-01-01T00:00:02+00:00"; store.save(later)
    earlier.created_at="2026-01-01T00:00:01+00:00"; store.save(earlier)

    children=store.children_of(parent.id,process="delivery")

    assert [child.id for child in children]==[earlier.id,later.id]


def test_run_history_is_persisted_with_artifacts_path(tmp_path: Path):
    store=TicketStore(tmp_path); store.init(); ticket=store.create("delivery","task","Traceability")

    store.record_run_event(ticket,run_id="run-42",stage_id="development",event="started",model="gpt-5.4",reasoning_effort="high")
    store.save(ticket)

    reloaded=store.get(ticket.id)

    assert reloaded.run_history==[{
        "run_id":"run-42",
        "stage":"development",
        "event":"started",
        "timestamp":reloaded.run_history[0]["timestamp"],
        "artifacts_path":".vibe/runs/run-42",
        "model":"gpt-5.4",
        "reasoning_effort":"high",
    }]
