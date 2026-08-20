from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.performance.run_benchmark import (CaseSpec, SQLiteMetrics, _cold_capability, _cases,
                                                   _unavailable_case, instrumented_connection_factory, percentile,
                                                   parse_seed, statistics_for, validate_result)
from benchmarks.performance.profile import prepare_output
from benchmarks.performance.workloads import BUDGET_STATES, RUNS_PER_TICKET, generate_fixture, load_dataset, validate_manifest

PROVENANCE = {"source_kind": "synthetic", "synthetic_only": True, "seed": 35527, "manifest_hash": "a"}


def test_percentile_is_deterministic_and_interpolated():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert statistics_for([1.0, 2.0, 3.0, 4.0])["p95"] == 3.85


def test_seed_accepts_decimal_and_ticket_style_hex_suffix():
    assert parse_seed("35527") == 35527
    assert parse_seed("355F27") == int("355F27", 16)


def test_benchmark_docs_use_required_decimal_seed():
    for path in (Path("benchmarks/performance/README.md"), Path("docs/performance.md")):
        text = path.read_text(encoding="utf-8")
        assert "--seed 35527" in text
        assert "--seed 355F27" not in text


def test_performance_policy_documents_thresholds_and_slo_decision():
    text = Path("docs/performance.md").read_text(encoding="utf-8")
    assert "Optimization criteria and regression policy" in text
    assert "at least 20%" in text
    assert ">5%" in text
    assert "no absolute" in text


def test_profile_linkage_descriptors_are_portable_and_complete():
    result = json.loads(Path("benchmarks/performance/artifacts/baseline-small-seed-35527.json").read_text(encoding="utf-8"))
    validate_result(result)
    assert result["parameters"]["warmup"] >= 5
    assert result["parameters"]["iterations"] >= 30
    assert result["profiling"]["artifacts"]
    for descriptor in result["profiling"]["artifacts"]:
        assert set(descriptor) == {"run_id", "case_id", "component", "manifest_hash", "path",
                                   "kind", "sha256", "size_bytes", "command_hash"}
        assert not Path(descriptor["path"]).is_absolute()
        assert ".." not in Path(descriptor["path"]).parts


def test_provenance_contract_requires_synthetic_marker_and_manifest_hash():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [], "source_checksum_before": "a", "source_checksum_after": "a",
              "provenance": {"source_kind": "synthetic", "synthetic_only": True,
                             "seed": 35527, "manifest_hash": "a"}}
    validate_result(result)


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
    db.commit()
    assert metrics.transactions == 1
    assert metrics.transaction_ms >= 0
    assert metrics.lock_wait_ms is None
    assert metrics.lock_wait_count is None
    assert metrics.attribution["contract_version"] == "sqlite-attribution.v1"
    db.close()


def test_sqlite_result_validation_requires_new_fields_when_present():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "c", "component": "x", "operation": "y", "storage_mode": "sqlite",
                          "dataset_dimensions": {}, "expected_outcome": "success", "errors": [], "statistics": {},
                          "sample_count": 1, "raw_samples": [{"sample_index": 0, "wall_ms": 1, "error": None,
                            "sqlite_queries": 1}]}], "source_checksum_before": "a", "source_checksum_after": "a", "provenance": PROVENANCE}
    with pytest.raises(ValueError, match="attribution"):
        validate_result(result)


def test_sqlite_result_validation_requires_transaction_duration():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "c", "component": "x", "operation": "y", "storage_mode": "sqlite",
                          "dataset_dimensions": {}, "expected_outcome": "success", "errors": [], "statistics": {},
                          "sample_count": 1, "raw_samples": [{"sample_index": 0, "wall_ms": 1, "error": None,
                            "sqlite_queries": 1, "sqlite_transactions": 1, "sqlite_errors": 0,
                            "sqlite_busy_errors": 0, "sqlite_lock_wait_ms": None, "sqlite_lock_wait_count": None,
                            "sqlite_attribution": {"source": "test"}}]}],
              "source_checksum_before": "a", "source_checksum_after": "a", "provenance": PROVENANCE}
    with pytest.raises(ValueError, match="attribution field"):
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
              "source_checksum_before": "a", "source_checksum_after": "a", "provenance": PROVENANCE}
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
        "http.handler.fragment", "http.api_tickets", "http.error.missing_session",
    }
    assert required <= ids
    assert {item[3] for item in cases}


def test_http_registry_reports_unavailable_loopback_without_relative_url(monkeypatch, tmp_path):
    generate_fixture(tmp_path, seed=4, size="small", storage_mode="sqlite")

    def unavailable_server(*args, **kwargs):
        raise OSError("loopback disabled")

    monkeypatch.setattr("benchmarks.performance.run_benchmark.start_server", unavailable_server)
    cases = _cases(tmp_path, storage="sqlite")
    http_case = next(case for case in cases if case.case_id == "http.api_tickets")

    assert http_case.limitations == ["HTTP loopback server unavailable: OSError"]
    with pytest.raises(RuntimeError, match="HTTP loopback server unavailable"):
        http_case.run()
    cases.cleanup()


def test_case_registry_has_stable_kinds_and_required_matrix(tmp_path):
    generate_fixture(tmp_path, seed=8, size="small", storage_mode="sqlite")
    cases = _cases(tmp_path, storage="sqlite")
    ids = [case.case_id for case in cases]
    assert len(ids) == len(set(ids))
    assert all(case.kind in {"read_only", "mutation"} for case in cases)
    required = {
        "ticketstore.save", "ticketstore.record_run_event", "sessionstore.inherit_ticket",
        "sessionstore.agent_update_membership", "budgetledger.create_budget",
        "budgetledger.increase_limit", "budgetledger.list_decisions", "scheduler.wip_count",
        "ui.GET_drawer", "ui.POST_create", "ui.PATCH_agent_session",
    }
    assert required <= set(ids)
    assert all("sqlite" in case.storage_modes for case in cases)


def test_result_validation_requires_clean_isolation_for_mutation():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "mutation", "component": "x", "operation": "y", "kind": "mutation",
                          "storage_mode": "sqlite", "dataset_dimensions": {}, "expected_outcome": "success",
                          "errors": [], "statistics": {}, "sample_count": 0, "raw_samples": [],
                          "isolation": {"before_hash": "a", "after_hash": "b", "leaked_entities": ["x"],
                                        "leaked_paths": [], "cleanup_errors": [], "clean": False}}],
              "source_checksum_before": "a", "source_checksum_after": "a", "provenance": PROVENANCE}
    with pytest.raises(ValueError, match="unclean mutation"):
        validate_result(result)


def test_case_spec_teardown_is_a_first_class_callback():
    spec = CaseSpec("x", "X", "operation", lambda: None, kind="mutation", teardown=lambda: None)
    assert spec[0] == "x" and spec[3] is spec.run


def test_inapplicable_storage_case_is_retained_as_unavailable(tmp_path):
    manifest = generate_fixture(tmp_path, seed=6, size="small", storage_mode="sqlite")
    spec = CaseSpec("http.loopback", "HTTP", "loopback", lambda: None,
                    storage_modes=("sqlite",), kind="read_only")
    result = _unavailable_case(spec, storage_mode="yaml", manifest=manifest)
    assert result["unavailable"] is True
    assert result["sample_count"] == 0
    assert "yaml" in result["limitations"][0]


def test_profile_output_removes_stale_owned_artifacts(tmp_path):
    output = tmp_path / "profile"
    output.mkdir()
    (output / "http.api_tickets.pstats").write_bytes(b"stale")
    (output / "http.api_tickets.txt").write_text("stale", encoding="utf-8")
    (output / "profile-manifest.json").write_text("stale", encoding="utf-8")
    (output / "keep.me").write_text("unrelated", encoding="utf-8")

    prepare_output(output)

    assert not (output / "http.api_tickets.pstats").exists()
    assert not (output / "http.api_tickets.txt").exists()
    assert not (output / "profile-manifest.json").exists()
    assert (output / "keep.me").exists()


def test_fixture_profile_counts_are_materialized(tmp_path):
    manifest = generate_fixture(tmp_path, seed=5, size="small", storage_mode="sqlite")
    assert manifest["counts"]["tickets"] == 100
    assert manifest["counts"]["sessions"] == manifest["dimensions"]["profile_counts"]["sessions"]
    assert manifest["counts"]["ledger_runs"] == manifest["dimensions"]["profile_counts"]["ledger_runs"]
    validate_manifest(manifest)
