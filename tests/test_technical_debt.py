import json
from pathlib import Path

import pytest
import yaml

from vibe_orchestrator.technical_debt import CONTRACT_VERSION, TechnicalDebtError, parse_technical_debt, preflight_technical_debt
from vibe_orchestrator.tickets import TicketStore


def _details(**overrides):
    fields = {"problem": "Сложная ветка", "evidence": {"path": "README.md", "identifier": "Traceability MVP", "observation": "Наблюдение"}, "impact": "Дорого сопровождать", "suggested_scope": "Выделить adapter", "source_ticket": "DISC-ABC123", "source_stage": "technical_analysis", "source_run": "run-1", "type": "task", "urgency": "medium", "priority": 10}
    fields.update(overrides)
    return yaml.safe_dump({"tech_debt_candidates": {"version": CONTRACT_VERSION, "candidates": [fields]}}, allow_unicode=True)


def _verified(_content: str, _identifier: str, _observation: str) -> bool:
    return True


def test_missing_key_and_empty_list_are_noops():
    assert parse_technical_debt("summary only") == []
    assert parse_technical_debt(yaml.safe_dump({"tech_debt_candidates": {"version": CONTRACT_VERSION, "candidates": []}})) == []


@pytest.mark.parametrize("field", ["problem", "impact", "suggested_scope", "source_ticket", "source_stage", "source_run"])
def test_required_fields_have_deterministic_paths(field):
    with pytest.raises(TechnicalDebtError) as caught:
        parse_technical_debt(_details(**{field: "  "}))
    assert caught.value.path == f"tech_debt_candidates.candidates[0].{field}"


@pytest.mark.parametrize("field,value", [("type", "bug"), ("urgency", "urgent"), ("priority", True), ("priority", -1)])
def test_enums_and_priority_are_strict(field, value):
    with pytest.raises(TechnicalDebtError) as caught:
        parse_technical_debt(_details(**{field: value}))
    assert caught.value.code == "TECH_DEBT_INVALID"


def test_preflight_checks_source_and_evidence_read_only(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    source = store.create("discovery", "idea", "Источник", status="technical_analysis")
    source.run_history.append({"run_id": "run-1", "stage": "technical_analysis", "event": "completed"})
    store.save(source)
    run_dir = tmp_path / ".vibe" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text('{"run_id":"run-1","ticket_id":"%s","stage":"technical_analysis"}' % source.id, encoding="utf-8")
    candidates = parse_technical_debt(_details(source_ticket=source.id))
    assert preflight_technical_debt(candidates, project=tmp_path, ticket_store=store, observation_verifier=_verified) == candidates
    with pytest.raises(TechnicalDebtError) as caught:
        preflight_technical_debt(parse_technical_debt(_details(source_ticket="DISC-MISSING")), project=tmp_path, ticket_store=store)
    assert caught.value.code == "TECH_DEBT_SOURCE_NOT_FOUND"


@pytest.mark.parametrize("manifest_metadata", [{"run_id": "run-2"}, {}])
def test_preflight_rejects_mismatched_or_missing_run_id(tmp_path: Path, manifest_metadata):
    store = TicketStore(tmp_path)
    store.init()
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    source = store.create("discovery", "idea", "Источник", status="technical_analysis")
    source.run_history.append({"run_id": "run-1", "stage": "technical_analysis", "event": "completed"})
    store.save(source)
    run_dir = tmp_path / ".vibe" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    manifest_metadata.update({"ticket_id": source.id, "stage": "technical_analysis"})
    (run_dir / "run.json").write_text(json.dumps(manifest_metadata), encoding="utf-8")

    with pytest.raises(TechnicalDebtError) as caught:
        preflight_technical_debt(parse_technical_debt(_details(source_ticket=source.id)), project=tmp_path, ticket_store=store)

    assert caught.value.code == "TECH_DEBT_SOURCE_MISMATCH"
    assert caught.value.path == "tech_debt_candidates.candidates[0].source_run"
    assert caught.value.envelope["contract_version"] == "orchestrator.errors.v1"
    if manifest_metadata.get("run_id") == "run-2":
        assert caught.value.envelope["details"] == {"expected": "run-1", "actual": "run-2"}
    else:
        assert caught.value.envelope["details"] == {"expected": "run-1"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("ticket_id", None),
        ("ticket_id", ""),
        ("ticket_id", "   "),
        ("ticket_id", 123),
        ("stage", None),
        ("stage", ""),
        ("stage", "   "),
        ("stage", 123),
    ],
)
def test_preflight_rejects_incomplete_manifest_identity(tmp_path: Path, field, value):
    store = TicketStore(tmp_path)
    store.init()
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    source = store.create("discovery", "idea", "Источник", status="technical_analysis")
    source.run_history.append({"run_id": "run-1", "ticket_id": source.id, "stage": "technical_analysis", "event": "completed"})
    store.save(source)
    run_dir = tmp_path / ".vibe" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    metadata = {"run_id": "run-1", "ticket_id": source.id, "stage": "technical_analysis"}
    metadata[field] = value
    (run_dir / "run.json").write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(TechnicalDebtError) as caught:
        preflight_technical_debt(parse_technical_debt(_details(source_ticket=source.id)), project=tmp_path, ticket_store=store)

    assert caught.value.code == "TECH_DEBT_SOURCE_MISMATCH"
    assert caught.value.path == "tech_debt_candidates.candidates[0].source_run"


def test_preflight_uses_history_only_when_manifest_is_absent(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    source = store.create("discovery", "idea", "Источник", status="technical_analysis")
    source.run_history.append({"run_id": "run-1", "ticket_id": source.id, "stage": "technical_analysis", "event": "completed"})
    store.save(source)

    candidates = parse_technical_debt(_details(source_ticket=source.id))
    assert preflight_technical_debt(candidates, project=tmp_path, ticket_store=store, observation_verifier=_verified) == candidates


@pytest.mark.parametrize(
    "field,value",
    [("ticket_id", None), ("ticket_id", ""), ("ticket_id", "   "), ("ticket_id", 123),
     ("stage", None), ("stage", ""), ("stage", "   "), ("stage", 123), ("stage", "review")],
)
def test_preflight_rejects_incomplete_history_identity_without_trying_another_entry(tmp_path: Path, field, value):
    store = TicketStore(tmp_path)
    store.init()
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    source = store.create("discovery", "idea", "Источник", status="technical_analysis")
    source.run_history.extend([
        {"run_id": "run-1", "ticket_id": source.id, "stage": "technical_analysis", "event": "started"},
        {"run_id": "run-1", "ticket_id": source.id, "stage": "technical_analysis", "event": "completed"},
    ])
    source.run_history[-1][field] = value
    store.save(source)

    with pytest.raises(TechnicalDebtError) as caught:
        preflight_technical_debt(parse_technical_debt(_details(source_ticket=source.id)), project=tmp_path, ticket_store=store, observation_verifier=_verified)

    assert caught.value.code == "TECH_DEBT_SOURCE_MISMATCH"
    assert caught.value.path == "tech_debt_candidates.candidates[0].source_run"


def test_preflight_requires_observation_verifier(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    source = store.create("discovery", "idea", "Источник", status="technical_analysis")
    source.run_history.append({"run_id": "run-1", "ticket_id": source.id, "stage": "technical_analysis", "event": "completed"})
    store.save(source)

    with pytest.raises(TechnicalDebtError) as caught:
        preflight_technical_debt(parse_technical_debt(_details(source_ticket=source.id)), project=tmp_path, ticket_store=store)

    assert caught.value.code == "TECH_DEBT_PREFLIGHT_UNAVAILABLE"
    assert caught.value.path == "tech_debt_candidates.candidates[0].evidence.observation"


@pytest.mark.parametrize("verdict,code", [(False, "TECH_DEBT_SOURCE_MISMATCH"), (True, None)])
def test_preflight_uses_observation_verifier_verdict(tmp_path: Path, verdict, code):
    store = TicketStore(tmp_path)
    store.init()
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    source = store.create("discovery", "idea", "Источник", status="technical_analysis")
    source.run_history.append({"run_id": "run-1", "ticket_id": source.id, "stage": "technical_analysis", "event": "completed"})
    store.save(source)
    calls = []

    def verifier(content: str, identifier: str, observation: str) -> bool:
        calls.append((content, identifier, observation))
        return verdict

    candidates = parse_technical_debt(_details(source_ticket=source.id))
    if code:
        with pytest.raises(TechnicalDebtError) as caught:
            preflight_technical_debt(candidates, project=tmp_path, ticket_store=store, observation_verifier=verifier)
        assert caught.value.code == code
        assert caught.value.path == "tech_debt_candidates.candidates[0].evidence.observation"
    else:
        assert preflight_technical_debt(candidates, project=tmp_path, ticket_store=store, observation_verifier=verifier) == candidates
    assert calls == [("Traceability MVP\n", "Traceability MVP", "Наблюдение")]
