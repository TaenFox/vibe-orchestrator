from __future__ import annotations

import json
from pathlib import Path

import json
import pytest

from benchmarks.performance.run_benchmark import (SQLiteMetrics, _cold_capability, _cases, _run_case,
                                                   instrumented_connection_factory, percentile,
                                                   statistics_for, validate_result)
from benchmarks.performance.workloads import BUDGET_STATES, RUNS_PER_TICKET, generate_fixture, load_dataset, validate_manifest


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


def test_sqlite_metrics_reset_and_classify_busy_errors(tmp_path):
    metrics = SQLiteMetrics()
    connect = instrumented_connection_factory(metrics)
    db = connect(tmp_path / "metrics.sqlite3")
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    metrics.reset()
    db.execute("INSERT INTO items DEFAULT VALUES")
    assert metrics.queries >= 1
    assert metrics.errors == 0
    assert metrics.lock_wait_ms is None
    assert metrics.lock_wait_count is None
    assert metrics.attribution["contract_version"] == "sqlite-attribution.v1"
    db.close()


def test_sqlite_result_validation_requires_new_fields_when_present():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "c", "component": "x", "operation": "y", "storage_mode": "sqlite",
                          "dataset_dimensions": {}, "expected_outcome": "success", "errors": [], "statistics": {},
                          "sample_count": 1, "raw_samples": [{"sample_index": 0, "wall_ms": 1, "error": None,
                            "sqlite_queries": 1}]}], "source_checksum_before": "a", "source_checksum_after": "a"}
    with pytest.raises(ValueError, match="attribution"):
        validate_result(result)


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


def test_result_validation_rejects_mutated_source_and_sample_mismatch():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "c", "component": "x", "operation": "y", "storage_mode": "sqlite",
                          "dataset_dimensions": {}, "expected_outcome": "success", "errors": [], "statistics": {},
                          "sample_count": 0, "raw_samples": []}],
              "source_checksum_before": "a", "source_checksum_after": "a"}
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
        "budgetledger.concurrency.write_contention",
        "http.handler.fragment", "http.api_tickets", "http.error.missing_session",
    }
    assert required <= ids
    assert {item[3] for item in cases}


def test_sqlite_store_cases_have_attributed_query_plans(tmp_path):
    generate_fixture(tmp_path, seed=4, size="small", storage_mode="sqlite")
    cases = {item[0]: item[3] for item in _cases(tmp_path, storage="sqlite")}
    case_ids = ("ticketstore.list.delivery", "ticketstore.list.all", "ticketstore.get.hit",
                "ticketstore.get.miss", "ticketstore.children_of", "sessionstore.list",
                "sessionstore.get", "sessionstore.create", "sessionstore.activate",
                "sessionstore.complete", "sessionstore.cancel", "sessionstore.add_membership",
                "sessionstore.remove_membership", "sessionstore.membership_validation.error",
                "sessionstore.validation.overlap.error")
    expected_labels = {
        "ticketstore.list.delivery": {"tickets.process list"},
        "ticketstore.list.all": {"tickets ordered list"},
        "ticketstore.get.hit": {"tickets.ticket_id lookup"},
        "ticketstore.get.miss": {"tickets.ticket_id lookup"},
        "ticketstore.children_of": {"tickets ordered list"},
        "sessionstore.list": {"sessions ordered list"},
        "sessionstore.get": {"sessions.session_id lookup"},
        "sessionstore.membership_validation.error": {"tickets.ticket_id lookup"},
        "sessionstore.validation.overlap.error": {"tickets.ticket_id lookup"},
        "sessionstore.create": {"tickets.ticket_id lookup", "sessions.session_id lookup"},
        "sessionstore.activate": {"tickets.ticket_id lookup", "sessions.session_id lookup"},
        "sessionstore.complete": {"tickets.ticket_id lookup", "sessions.session_id lookup"},
        "sessionstore.cancel": {"tickets.ticket_id lookup", "sessions.session_id lookup"},
        "sessionstore.add_membership": {"tickets.ticket_id lookup", "sessions.session_id lookup"},
        "sessionstore.remove_membership": {"tickets.ticket_id lookup", "sessions.session_id lookup"},
    }
    for case_id in case_ids:
        plans = getattr(cases[case_id], "_sqlite_plans")
        assert plans, case_id
        assert all(isinstance(plan["query"], str) and isinstance(plan["detail"], list) for plan in plans)
        assert all(all(isinstance(detail, str) for detail in plan["detail"]) for plan in plans)
        assert {plan["query"] for plan in plans} == expected_labels[case_id]
    assert any("USING INDEX" in detail or "USING INTEGER PRIMARY KEY" in detail
               for detail in getattr(cases["ticketstore.get.hit"], "_sqlite_plans")[0]["detail"])
    assert any("sessions" in plan["query"] for plan in getattr(cases["sessionstore.get"], "_sqlite_plans"))
    for case_id in expected_labels:
        if case_id.startswith("sessionstore.") and case_id not in {"sessionstore.list", "sessionstore.get"}:
            if case_id.startswith("sessionstore.validation.") or case_id == "sessionstore.membership_validation.error":
                assert not getattr(cases[case_id], "_limitations")
            else:
                assert any("lifecycle read families" in item for item in getattr(cases[case_id], "_limitations"))


def test_session_validation_plan_attribution_matches_executed_query_family(tmp_path):
    """Regression: a validation failure must not inherit a session lookup plan."""
    generate_fixture(tmp_path, seed=4, size="small", storage_mode="sqlite")
    cases = {item[0]: item[3] for item in _cases(tmp_path, storage="sqlite")}

    overlap = getattr(cases["sessionstore.validation.overlap.error"], "_sqlite_plans")
    missing_membership = getattr(cases["sessionstore.membership_validation.error"], "_sqlite_plans")
    assert [plan["query"] for plan in overlap] == ["tickets.ticket_id lookup"]
    assert [plan["query"] for plan in missing_membership] == ["tickets.ticket_id lookup"]
    assert all("sessions" not in detail.lower() for plan in overlap for detail in plan["detail"])


def test_yaml_load_path_has_no_sqlite_plans(tmp_path):
    generate_fixture(tmp_path, seed=4, size="small", storage_mode="yaml")
    cases = {item[0]: item[3] for item in _cases(tmp_path, storage="yaml")}
    assert getattr(cases["ticketstore.load_path"], "_sqlite_plans") == []
    assert getattr(cases["sessionstore.load_path"], "_sqlite_plans") == []


def test_sqlite_load_path_is_explicitly_non_sqlite(tmp_path):
    manifest = generate_fixture(tmp_path, seed=4, size="small", storage_mode="sqlite")
    cases = {item[0]: item[3] for item in _cases(tmp_path, storage="sqlite")}
    for case_id in ("ticketstore.load_path", "sessionstore.load_path"):
        fn = cases[case_id]
        assert getattr(fn, "_sqlite_metrics") is None
        assert getattr(fn, "_sqlite_plans") == []
        result = _run_case(case_id, "TicketStore" if case_id.startswith("ticket") else "SessionStore",
                           "load_path", fn, tmp_path, 0, 1, False,
                           storage_mode="sqlite", manifest=manifest)
        assert result["sqlite_explain_query_plan"] == []
        assert any("case does not use SQLite" in item for item in result["limitations"])
        sample = result["raw_samples"][0]
        assert sample["sqlite_queries"] is None
        assert sample["sqlite_attribution"]["source"] is None
        validate_result({"schema_version": "performance-result.v2", "run_id": "r",
                         "dataset_manifest": {}, "cases": [result],
                         "source_checksum_before": "a", "source_checksum_after": "a"})


def test_contention_case_observes_a_released_writer_lock(tmp_path):
    generate_fixture(tmp_path, seed=4, size="small", storage_mode="sqlite")
    cases = {item[0]: item[3] for item in _cases(tmp_path, storage="sqlite")}
    result = cases["budgetledger.concurrency.write_contention"]()
    assert result["state"] == "reserved_pending_start"
    assert result["lock_wait_ms"] >= 15


def test_result_validation_rejects_invalid_sqlite_metric_types():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "c", "component": "x", "operation": "y", "storage_mode": "sqlite",
                          "dataset_dimensions": {}, "expected_outcome": "success", "errors": [], "statistics": {},
                          "sample_count": 1, "raw_samples": [{"sample_index": 0, "wall_ms": 1, "error": None,
                            "sqlite_queries": 1, "sqlite_transactions": 1, "sqlite_errors": 0,
                            "sqlite_busy_errors": 0, "sqlite_lock_wait_ms": "slow", "sqlite_lock_wait_count": 1,
                            "sqlite_attribution": {"source": "test"}}]}],
              "source_checksum_before": "a", "source_checksum_after": "a"}
    with pytest.raises(ValueError, match="lock wait"):
        validate_result(result)


def test_fixture_profile_counts_are_materialized(tmp_path):
    manifest = generate_fixture(tmp_path, seed=5, size="small", storage_mode="sqlite")
    assert manifest["counts"]["tickets"] == 100
    assert manifest["counts"]["sessions"] == manifest["dimensions"]["profile_counts"]["sessions"]
    assert manifest["counts"]["ledger_runs"] == manifest["dimensions"]["profile_counts"]["ledger_runs"]
    validate_manifest(manifest)
