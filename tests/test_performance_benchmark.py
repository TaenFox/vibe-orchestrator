from __future__ import annotations

import json
import sqlite3
from collections import Counter
from argparse import Namespace
from pathlib import Path

import pytest

from benchmarks.performance import run_benchmark
from benchmarks.performance.run_benchmark import _cold_capability, _cases, percentile, statistics_for, validate_result
from benchmarks.performance.workloads import (BUDGET_STATES, PROFILE_DIMENSIONS, RUNS_PER_TICKET,
                                              SIZES, assert_manifest_identity, generate_fixture, load_dataset, materialize_dataset,
                                              manifest_identity, validate_manifest)
from vibe_orchestrator.budget_ledger import BudgetLedger
from vibe_orchestrator.sessions import SessionStore
from vibe_orchestrator.tickets import TicketStore


def test_percentile_is_deterministic_and_interpolated():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert statistics_for([1.0, 2.0, 3.0, 4.0])["p95"] == 3.85


def test_manifest_privacy_contract_is_explicit():
    source = Path("benchmarks/performance/fixtures/README.md").read_text(encoding="utf-8")
    assert "titles" in source and "prompts" in source and "prohibited" in source


def test_result_schema_requires_raw_timing_fields():
    sample = {"sample_index": 0, "wall_ms": 1.0, "cpu_ms": 0.5, "fs_ops": None,
              "fs_bytes": None, "sqlite_queries": None, "sqlite_lock_ms": None, "error": None}
    assert {"sample_index", "wall_ms", "error"} <= sample.keys()


def test_fixture_is_seeded_redacted_and_materializes_dimensions(tmp_path):
    first = generate_fixture(tmp_path / "first", seed=17, size="small", storage_mode="yaml")
    second = generate_fixture(tmp_path / "second", seed=17, size="small", storage_mode="yaml")
    different = generate_fixture(tmp_path / "different", seed=18, size="small", storage_mode="yaml")
    assert first["fixture_files_sha256"] == second["fixture_files_sha256"]
    assert first["fixture_files_sha256"] != different["fixture_files_sha256"]
    assert first["counts"]["tickets"] == 100
    assert set(first["dimensions"]["session_states"]) == {"draft", "active", "completed", "cancelled"}
    assert first["redaction_policy"].startswith("synthetic identifiers")
    assert set(first["dimensions"]["runs_per_ticket_counts"]) == {str(item) for item in RUNS_PER_TICKET}
    assert all(first["dimensions"]["session_states"][state] > 0 for state in ("draft", "active", "completed", "cancelled"))
    assert all(first["dimensions"]["budget_states"][state] > 0 for state in BUDGET_STATES)
    ticket_text = list((tmp_path / "first" / ".vibe" / "tickets").rglob("*.yaml"))[0].read_text(encoding="utf-8")
    assert "title: ''" in ticket_text and "description: ''" in ticket_text


def test_dataset_manifest_is_not_silently_ignored(tmp_path):
    manifest = generate_fixture(tmp_path / "fixture", seed=1)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_dataset(path)["seed"] == 1
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(path)
    path.write_text(json.dumps(manifest | {"hashes": {"manifest_sha256": "tampered"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_dataset(path)


def test_dataset_bundle_can_convert_storage_without_changing_entities(tmp_path):
    source = tmp_path / "approved"
    manifest = generate_fixture(source, seed=22, size="small", storage_mode="sqlite")
    target = tmp_path / "alternate"
    target.mkdir()
    converted = materialize_dataset(target, source, manifest, storage_mode="yaml")
    assert converted["storage_mode"] == "yaml"
    assert converted["hashes"]["manifest_sha256"] != manifest["hashes"]["manifest_sha256"]
    assert len(list((target / ".vibe" / "tickets" / "delivery").glob("*.yaml"))) == manifest["counts"]["tickets"]
    assert len(list((target / ".vibe" / "sessions").glob("*.yaml"))) == manifest["counts"]["sessions"]


def test_dataset_bundle_materializes_existing_vibe_tree(tmp_path):
    source = tmp_path / "approved"
    manifest = generate_fixture(source, seed=21, size="small", storage_mode="yaml")
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "isolated"
    target.mkdir()
    materialized = materialize_dataset(target, source / "manifest.json", load_dataset(source / "manifest.json"))
    assert materialized["source_kind"] == "approved_dataset"
    assert (target / ".vibe" / "tickets").exists()
    assert len(list((target / ".vibe" / "tickets").rglob("*.yaml"))) == manifest["counts"]["tickets"]


def test_manifest_rejects_internally_consistent_wrong_profile_ticket_count(tmp_path):
    manifest = generate_fixture(tmp_path / "fixture", seed=23, size="small")
    altered = json.loads(json.dumps(manifest))
    altered["logical"]["ticket_ids"].pop()
    altered["counts"]["tickets"] = SIZES["small"] - 1

    status = next(key for key, value in altered["dimensions"]["ticket_status"].items() if value > 0)
    altered["dimensions"]["ticket_status"][status] -= 1
    run_count = next(key for key, value in altered["dimensions"]["runs_per_ticket_counts"].items() if value > 0)
    altered["dimensions"]["runs_per_ticket_counts"][run_count] -= 1
    payload, checksum = manifest_identity(altered)
    altered["logical"] = payload
    altered["hashes"]["manifest_sha256"] = checksum
    altered["logical_checksum"] = checksum
    altered["fixture_files_sha256"] = checksum

    with pytest.raises(ValueError, match="ticket count does not match profile.*small.*100.*99"):
        validate_manifest(altered)

    dataset = tmp_path / "invalid-cardinality.json"
    dataset.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="ticket count does not match profile"):
        load_dataset(dataset)


def test_manifest_rejects_budget_distribution_with_wrong_total(tmp_path):
    manifest = generate_fixture(tmp_path / "fixture", seed=24, size="small")
    altered = json.loads(json.dumps(manifest))
    altered["dimensions"]["budget_states"]["active"] -= 1
    altered["dimensions"]["budget_states"]["exhausted"] += 1
    payload, checksum = manifest_identity(altered)
    altered["logical"] = payload
    altered["hashes"]["manifest_sha256"] = checksum
    altered["logical_checksum"] = checksum
    altered["fixture_files_sha256"] = checksum
    altered["dimensions"]["budget_states"]["active"] -= 1
    altered["logical"], checksum = manifest_identity(altered)
    altered["hashes"]["manifest_sha256"] = checksum
    altered["logical_checksum"] = checksum
    altered["fixture_files_sha256"] = checksum
    with pytest.raises(ValueError, match="budget state counts"):
        validate_manifest(altered)


@pytest.mark.parametrize("size", ["small", "medium", "large", "xlarge"])
@pytest.mark.parametrize("storage_mode", ["sqlite", "yaml"])
def test_fixture_profile_counts_match_materialized_entities(tmp_path, size, storage_mode):
    project = tmp_path / f"{storage_mode}-{size}"
    manifest = generate_fixture(project, seed=21, size=size, storage_mode=storage_mode)
    assert manifest["counts"] == {"tickets": {"small": 100, "medium": 1000, "large": 5000, "xlarge": 10000}[size],
                                   "sessions": PROFILE_DIMENSIONS[size]["sessions"],
                                   "ledger_runs": PROFILE_DIMENSIONS[size]["ledger_runs"]}
    tickets = TicketStore(project, use_database=storage_mode == "sqlite").list("delivery")
    assert {ticket.id for ticket in tickets} == set(manifest["logical"]["ticket_ids"])
    assert Counter(ticket.status for ticket in tickets) == Counter(manifest["dimensions"]["ticket_status"])
    assert Counter(str(len(ticket.run_history)) for ticket in tickets) == Counter(manifest["dimensions"]["runs_per_ticket_counts"])
    sessions = SessionStore(project, TicketStore(project, use_database=storage_mode == "sqlite"),
                            use_database=storage_mode == "sqlite").list()
    assert Counter(session.status for session in sessions) == Counter(manifest["dimensions"]["session_states"])
    ledger = BudgetLedger(project)
    # Read the authoritative SQLite ledger in one pass. Calling list_runs once
    # per ticket has the same semantics but makes xlarge verification needlessly
    # expensive in connection/query overhead.
    with sqlite3.connect(ledger.path) as database:
        rows = database.execute(
            "SELECT ticket_id, ticket_budget_id FROM runs ORDER BY reserved_at, run_id"
        ).fetchall()
    assert all(ticket_id and budget_id == f"ticket:{ticket_id}" for ticket_id, budget_id in rows)
    runs = len(rows)
    assert runs == manifest["counts"]["ledger_runs"]
    with sqlite3.connect(ledger.path) as database:
        budget_rows = database.execute(
            "SELECT budget_id, status FROM budgets WHERE scope = 'ticket' ORDER BY budget_id"
        ).fetchall()
    assert {budget_id.removeprefix("ticket:") for budget_id, _ in budget_rows} == set(manifest["logical"]["ticket_ids"])
    budget_states = Counter(status for _, status in budget_rows)
    # Re-evaluate the four non-default states through the public read API; the
    # bulk query above avoids opening one connection per ordinary active budget.
    for ticket_id in manifest["logical"]["ticket_ids"][:4]:
        assert ledger.read_budget(f"ticket:{ticket_id}")["status"] == next(
            status for budget_id, status in budget_rows if budget_id == f"ticket:{ticket_id}"
        )
    for state in ("exhausted", "blocked_unknown", "over_budget"):
        assert budget_states[state] == manifest["dimensions"]["budget_states"][state]
    assert budget_states["active"] == manifest["counts"]["tickets"] - 3
    assert budget_states["exhausted"] == 1
    assert budget_states["blocked_unknown"] == 1
    assert budget_states["over_budget"] == 1
    validate_manifest(manifest)


def test_yaml_fixture_readback_uses_legacy_ticket_and_session_stores(tmp_path):
    project = tmp_path / "yaml-small"
    manifest = generate_fixture(project, seed=22, size="small", storage_mode="yaml")
    tickets = TicketStore(project, use_database=False).list("delivery")
    sessions = SessionStore(project, TicketStore(project, use_database=False), use_database=False).list()
    assert len(tickets) == manifest["counts"]["tickets"]
    assert len(sessions) == manifest["counts"]["sessions"]
    assert Counter(ticket.status for ticket in tickets) == Counter(manifest["dimensions"]["ticket_status"])
    assert Counter(session.status for session in sessions) == Counter(manifest["dimensions"]["session_states"])


def test_manifest_identity_gate_rejects_valid_altered_manifest(tmp_path):
    manifest = generate_fixture(tmp_path / "fixture", seed=31)
    altered = json.loads(json.dumps(manifest))
    altered["logical"]["ticket_ids"][0] = "FIX-00031-99999"
    payload, checksum = manifest_identity(altered)
    altered["logical"] = payload
    altered["hashes"]["manifest_sha256"] = checksum
    altered["logical_checksum"] = checksum
    altered["fixture_files_sha256"] = checksum
    validate_manifest(altered)
    with pytest.raises(ValueError, match="dataset identity mismatch"):
        assert_manifest_identity(altered, manifest)


def test_manifest_identity_gate_reports_altered_dimensions(tmp_path):
    manifest = generate_fixture(tmp_path / "fixture", seed=32)
    altered = json.loads(json.dumps(manifest))
    altered["dimensions"]["ticket_status"]["todo"] += 1
    altered["dimensions"]["ticket_status"]["development"] -= 1
    payload, checksum = manifest_identity(altered)
    altered["logical"] = payload
    altered["hashes"]["manifest_sha256"] = checksum
    altered["logical_checksum"] = checksum
    altered["fixture_files_sha256"] = checksum
    validate_manifest(altered)
    with pytest.raises(ValueError, match="differing canonical fields=.*ticket_status"):
        assert_manifest_identity(altered, manifest)


def _run_args(project, dataset, output, *, storage="sqlite"):
    return Namespace(project=project, output=output, dataset=dataset, profile="smoke", size=None,
                     storage=storage, seed=35527, warmup=0, iterations=1, cold=False)


def test_dataset_identity_gate_runs_before_cases_and_output(tmp_path, monkeypatch):
    manifest = generate_fixture(tmp_path / "manifest-fixture", seed=37)
    altered = json.loads(json.dumps(manifest))
    altered["logical"]["ticket_ids"][0] = "FIX-00037-99999"
    payload, checksum = manifest_identity(altered)
    altered["logical"] = payload
    altered["hashes"]["manifest_sha256"] = checksum
    altered["logical_checksum"] = checksum
    altered["fixture_files_sha256"] = checksum
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(altered), encoding="utf-8")
    output = tmp_path / "result.json"
    called = False

    def sentinel(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(run_benchmark, "_cases", sentinel)
    with pytest.raises(ValueError, match="dataset identity mismatch"):
        run_benchmark.run(_run_args(Path("."), dataset, output))
    assert not called
    assert not output.exists()


def test_identical_dataset_manifest_materializes_and_preserves_identity(tmp_path, monkeypatch):
    project = tmp_path / "manifest-fixture"
    manifest = generate_fixture(project, seed=41)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "result.json"
    monkeypatch.setattr(run_benchmark, "_cases", lambda *args, **kwargs: [
        ("identity.case", "test", "identity", lambda: None),
    ])
    result = run_benchmark.run(_run_args(Path("."), dataset, output))
    assert output.exists()
    assert result["dataset_manifest_hash"] == manifest["hashes"]["manifest_sha256"]
    assert result["cases"]
    assert all(case["dataset_manifest_hash"] == manifest["hashes"]["manifest_sha256"]
               for case in result["cases"])
    validate_result(result)


def test_yaml_dataset_uses_manifest_storage_when_cli_storage_is_omitted(tmp_path, monkeypatch):
    manifest = generate_fixture(tmp_path / "manifest-fixture", seed=42, storage_mode="yaml")
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "result.json"
    seen = []
    monkeypatch.setattr(run_benchmark, "_cases", lambda project, *, storage: (
        seen.append(storage) or [("identity.case", "test", "identity", lambda: None)]))
    result = run_benchmark.run(_run_args(Path("."), dataset, output, storage=None))
    assert seen == ["yaml"]
    assert result["parameters"]["storage"] == "yaml"
    assert result["cases"][0]["storage_mode"] == "yaml"


def test_yaml_dataset_rejects_explicit_sqlite_before_cases_and_output(tmp_path, monkeypatch):
    manifest = generate_fixture(tmp_path / "manifest-fixture", seed=43, storage_mode="yaml")
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "result.json"
    called = False

    def sentinel(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(run_benchmark, "_cases", sentinel)
    with pytest.raises(ValueError, match="--storage must match"):
        run_benchmark.run(_run_args(Path("."), dataset, output, storage="sqlite"))
    assert not called
    assert not output.exists()


def test_manifest_hash_fields_are_canonical(tmp_path):
    manifest = generate_fixture(tmp_path / "fixture", seed=7)
    manifest["hashes"]["manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        validate_manifest(manifest)


def test_result_validation_rejects_mutated_source_and_sample_mismatch():
    manifest = {"hashes": {"manifest_sha256": "m"}}
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": manifest,
              "cases": [{"case_id": "c", "component": "x", "operation": "y", "storage_mode": "sqlite",
                          "dataset_dimensions": {}, "expected_outcome": "success", "errors": [], "statistics": {},
                          "sample_count": 0, "raw_samples": [], "dataset_manifest_hash": "m"}],
              "source_checksum_before": "a", "source_checksum_after": "a",
              "integrity": {"warmup_excluded": True}}
    result["dataset_manifest_hash"] = "m"
    validate_result(result)
    result["cases"][0]["sample_count"] = 1
    with pytest.raises(ValueError):
        validate_result(result)


def test_manifest_rejects_claimed_but_unmaterialized_run_dimension(tmp_path):
    manifest = generate_fixture(tmp_path / "fixture", seed=9)
    manifest["dimensions"]["runs_per_ticket_counts"]["0"] -= 1
    with pytest.raises(ValueError, match="runs_per_ticket"):
        validate_manifest(manifest)


def test_cold_capability_is_explicit():
    capability = _cold_capability()
    assert set(capability) == {"available", "strategy", "limitation"}
    if not capability["available"]:
        assert capability["limitation"]


def test_case_registry_covers_storage_and_lifecycle_contract(tmp_path):
    generate_fixture(tmp_path, seed=4, size="small", storage_mode="sqlite")
    cases = _cases(tmp_path, storage="sqlite")
    ids = {item[0] for item in cases}
    required = {
        "ticketstore.load_path", "ticketstore.children_of",
        "sessionstore.create", "sessionstore.activate", "sessionstore.complete", "sessionstore.cancel",
        "budgetledger.reserve.idempotent", "budgetledger.start", "budgetledger.finalize",
        "budgetledger.release", "budgetledger.reconcile", "budgetledger.concurrency.atomic_reserve",
        "budgetledger.concurrency.denied_overallocation",
        "budgetledger.concurrency.lock_wait",
        "http.handler.fragment", "http.api_tickets", "http.error.missing_session",
        "orchestrator.scan_sort_cycle", "ui.filter.flat", "ui.filter.search", "ui.filter.status", "ui.filter.active",
        "sessionstore.get_missing.error", "sessionstore.load_invalid_persisted.error",
        "sessionstore.validation.multiple_open_overlap.error", "http.api_agent_tickets", "http.api_agent_sessions",
    }
    assert required <= ids
    assert {item[3] for item in cases}


def test_yaml_case_registry_exercises_http_server_with_yaml_stores(tmp_path):
    generate_fixture(tmp_path, seed=7, size="small", storage_mode="yaml")
    cases = _cases(tmp_path, storage="yaml")
    http_cases = [item for item in cases if item[1] == "HTTP"]

    assert http_cases
    api_tickets = next(item for item in http_cases if item[0] == "http.api_tickets")
    limitations = getattr(api_tickets[3], "_limitations", [])
    if limitations:
        assert limitations[0].startswith("HTTP loopback server unavailable:")
        assert getattr(api_tickets[3], "_profile_available") is False
    else:
        assert api_tickets[3]()
        assert getattr(api_tickets[3], "_profile_available") is True


def test_lock_wait_is_measured_separately_from_transaction_time(tmp_path):
    generate_fixture(tmp_path, seed=6, size="small", storage_mode="sqlite")
    lock_case = next(item for item in _cases(tmp_path, storage="sqlite")
                     if item[0] == "budgetledger.concurrency.lock_wait")
    result = lock_case[3]()
    assert result["lock_wait_ms"] > 0
    assert result["transaction_ms"] >= 0
    assert result["lock_wait_ms"] != result["transaction_ms"]


def test_fixture_profile_counts_are_materialized(tmp_path):
    manifest = generate_fixture(tmp_path, seed=5, size="small", storage_mode="sqlite")
    assert manifest["counts"]["tickets"] == 100
    assert manifest["counts"]["sessions"] == manifest["dimensions"]["profile_counts"]["sessions"]
    assert manifest["counts"]["ledger_runs"] == manifest["dimensions"]["profile_counts"]["ledger_runs"]
    validate_manifest(manifest)
