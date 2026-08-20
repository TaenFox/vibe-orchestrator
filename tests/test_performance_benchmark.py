from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import json
import pytest

from benchmarks.performance.run_benchmark import CaseRegistry, CaseSpec, _cold_capability, _cases, _run_case, percentile, run, statistics_for, validate_result
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
        "http.handler.fragment", "http.api_tickets", "http.error.missing_session",
    }
    assert required <= ids
    assert {item[3] for item in cases}


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
    assert next(case for case in cases if case.case_id == "budgetledger.reconcile").kind == "mutation"


def test_result_validation_requires_clean_isolation_for_mutation():
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "mutation", "component": "x", "operation": "y", "kind": "mutation",
                          "storage_mode": "sqlite", "dataset_dimensions": {}, "expected_outcome": "success",
                          "errors": [], "statistics": {}, "sample_count": 0, "raw_samples": [],
                          "isolation": {"before_hash": "a", "after_hash": "b", "leaked_entities": ["x"],
                                        "leaked_paths": [], "cleanup_errors": [], "clean": False}}],
              "source_checksum_before": "a", "source_checksum_after": "a"}
    with pytest.raises(ValueError, match="unclean mutation"):
        validate_result(result)


@pytest.mark.parametrize(
    ("expected_outcome", "error"),
    [("success", "RuntimeError"), ("error", None)],
)
def test_result_validation_requires_declared_outcome(expected_outcome, error):
    sample = {"sample_index": 2, "wall_ms": 1.0, "error": error}
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "outcome.case", "component": "x", "operation": "y",
                          "kind": "read_only", "storage_mode": "sqlite", "dataset_dimensions": {},
                          "expected_outcome": expected_outcome, "errors": [], "statistics": {},
                          "sample_count": 1, "raw_samples": [sample]}],
              "source_checksum_before": "a", "source_checksum_after": "a"}
    with pytest.raises(ValueError, match=r"outcome\.case.*sample_index 2"):
        validate_result(result)


@pytest.mark.parametrize("isolation_clean", [False, None])
def test_result_validation_requires_clean_isolation_for_each_read_only_sample(isolation_clean):
    sample = {"sample_index": 3, "wall_ms": 1.0, "error": None}
    if isolation_clean is not None:
        sample["isolation_clean"] = isolation_clean
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
              "cases": [{"case_id": "read.case", "component": "x", "operation": "y", "kind": "read_only",
                          "storage_mode": "sqlite", "dataset_dimensions": {}, "expected_outcome": "success",
                          "errors": [], "statistics": {}, "sample_count": 1, "raw_samples": [sample]}],
              "source_checksum_before": "a", "source_checksum_after": "a"}
    with pytest.raises(ValueError, match=r"read\.case.*sample_index 3"):
        validate_result(result)


def test_run_case_read_only_contamination_is_rejected_by_validation(tmp_path):
    root = tmp_path / ".vibe"
    root.mkdir()
    synthetic = root / "benchmark-read-only.yaml"

    def run():
        synthetic.write_text("state: leaked\n", encoding="utf-8")

    spec = CaseSpec("test.read_only_contamination", "test", "read", run)
    result = _run_case(spec, tmp_path, warmup=0, iterations=1, noisy=False,
                       storage_mode="sqlite", manifest={"dimensions": {}, "fixture_files_sha256": "test"})
    assert result["raw_samples"][0]["isolation_clean"] is False
    with pytest.raises(ValueError, match=r"test\.read_only_contamination.*sample_index 0"):
        validate_result({"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": {},
                         "cases": [result], "source_checksum_before": "a", "source_checksum_after": "a"})


def test_case_registry_cleanup_is_idempotent():
    calls = []
    registry = CaseRegistry(iter(()), lambda: calls.append(True))
    registry.cleanup()
    registry.cleanup()
    assert calls == [True]


def test_run_cleans_registry_when_sampling_raises(tmp_path, monkeypatch):
    source = tmp_path / "project"
    source.mkdir()
    cleanup_calls = []
    registry = CaseRegistry(iter([CaseSpec("test.case", "test", "operation", lambda: None)]),
                             lambda: cleanup_calls.append(True))
    monkeypatch.setattr("benchmarks.performance.run_benchmark._cases", lambda *_args, **_kwargs: registry)

    def fail_sampling(*_args, **_kwargs):
        raise RuntimeError("sampling failed")

    monkeypatch.setattr("benchmarks.performance.run_benchmark._run_case", fail_sampling)
    args = SimpleNamespace(project=source, output=tmp_path / "result.json", profile="smoke", size="small",
                            storage="yaml", warmup=0, iterations=1, seed=1, dataset=None, cold=False)
    with pytest.raises(RuntimeError, match="sampling failed"):
        run(args)
    assert cleanup_calls == [True]


def test_case_spec_teardown_is_a_first_class_callback():
    spec = CaseSpec("x", "X", "operation", lambda: None, kind="mutation", teardown=lambda: None)
    assert spec[0] == "x" and spec[3] is spec.run


def test_run_case_callback_exception_still_cleans_mutation(tmp_path):
    root = tmp_path / ".vibe"
    root.mkdir()
    synthetic = root / "benchmark-exception.yaml"
    teardown_calls = []

    def run():
        synthetic.write_text("state: leaked\n", encoding="utf-8")
        raise RuntimeError("callback failed")

    def teardown():
        teardown_calls.append(True)
        synthetic.unlink(missing_ok=True)

    spec = CaseSpec("test.mutation_exception", "test", "mutation", run,
                    kind="mutation", teardown=teardown)
    result = _run_case(spec, tmp_path, warmup=0, iterations=1, noisy=False,
                       storage_mode="sqlite", manifest={"dimensions": {}, "fixture_files_sha256": "test"})

    assert teardown_calls == [True]
    assert result["errors"] == [{"sample_index": 0, "type": "RuntimeError"}]
    assert result["isolation"]["before_hash"] == result["isolation"]["after_hash"]
    assert result["isolation"]["leaked_entities"] == []
    assert result["isolation"]["leaked_paths"] == []
    assert result["isolation"]["cleanup_errors"] == []
    assert result["isolation"]["clean"] is True


def test_run_case_warmup_uses_cleanup_and_does_not_move_baseline(tmp_path):
    root = tmp_path / ".vibe"
    root.mkdir()
    synthetic = root / "warmup.yaml"
    calls = []

    def setup():
        calls.append("setup")

    def run_callback():
        synthetic.write_text("state: warmup\n", encoding="utf-8")
        calls.append("run")

    def teardown():
        synthetic.unlink(missing_ok=True)
        calls.append("teardown")

    result = _run_case(CaseSpec("test.warmup", "test", "mutation", run_callback,
                                kind="mutation", setup=setup, teardown=teardown),
                       tmp_path, warmup=1, iterations=1, noisy=False, storage_mode="sqlite",
                       manifest={"dimensions": {}, "fixture_files_sha256": "test"})
    assert calls == ["setup", "run", "teardown", "setup", "run", "teardown"]
    assert result["isolation"]["clean"] is True


def test_run_case_warmup_teardown_runs_after_exception(tmp_path):
    root = tmp_path / ".vibe"
    root.mkdir()
    synthetic = root / "warmup-error.yaml"
    teardown_calls = []

    def run_callback():
        synthetic.write_text("state: leaked\n", encoding="utf-8")
        raise RuntimeError("expected")

    def teardown():
        teardown_calls.append(True)
        synthetic.unlink(missing_ok=True)

    result = _run_case(CaseSpec("test.warmup.error", "test", "mutation", run_callback,
                                kind="mutation", expected_outcome="error", teardown=teardown),
                       tmp_path, warmup=1, iterations=1, noisy=False, storage_mode="sqlite",
                       manifest={"dimensions": {}, "fixture_files_sha256": "test"})
    assert teardown_calls == [True, True]
    assert result["errors"] == [{"sample_index": 0, "type": "RuntimeError"}]
    assert result["isolation"]["clean"] is True


def test_run_case_rejects_unexpected_warmup_error(tmp_path):
    root = tmp_path / ".vibe"
    root.mkdir()

    def run_callback():
        raise RuntimeError("unexpected")

    with pytest.raises(ValueError, match="warmup execution failed.*RuntimeError"):
        _run_case(CaseSpec("test.warmup.unexpected", "test", "mutation", run_callback,
                           kind="mutation"), tmp_path, warmup=1, iterations=1,
                  noisy=False, storage_mode="sqlite",
                  manifest={"dimensions": {}, "fixture_files_sha256": "test"})


def test_budget_override_cases_use_isolated_budget(tmp_path):
    generate_fixture(tmp_path, seed=12, size="small", storage_mode="sqlite")
    cases = _cases(tmp_path, storage="sqlite")
    try:
        for case_id in ("budgetledger.increase_limit", "budgetledger.allow_overrun",
                        "budgetledger.set_status", "budgetledger.resolve_unknown",
                        "budgetledger.adjustment"):
            case = next(case for case in cases if case.case_id == case_id)
            result = _run_case(case, tmp_path, warmup=1, iterations=1, noisy=False,
                               storage_mode="sqlite", manifest={"dimensions": {}, "fixture_files_sha256": "test"})
            assert result["errors"] == []
            assert result["raw_samples"][0]["error"] is None
            assert result["isolation"]["clean"] is True
    finally:
        cases.cleanup()


def test_run_case_reports_only_real_teardown_failure(tmp_path):
    root = tmp_path / ".vibe"
    root.mkdir()
    synthetic = root / "benchmark-cleanup-failure.yaml"

    def run():
        synthetic.write_text("state: leaked\n", encoding="utf-8")
        raise RuntimeError("callback failed")

    def teardown():
        raise OSError("cleanup failed")

    spec = CaseSpec("test.mutation_cleanup_failure", "test", "mutation", run,
                    kind="mutation", teardown=teardown)
    result = _run_case(spec, tmp_path, warmup=0, iterations=1, noisy=False,
                       storage_mode="sqlite", manifest={"dimensions": {}, "fixture_files_sha256": "test"})

    assert result["errors"] == [{"sample_index": 0, "type": "RuntimeError"}]
    assert result["isolation"]["cleanup_errors"] == [{"sample_index": 0, "type": "OSError"}]
    assert result["isolation"]["clean"] is False


def test_fixture_profile_counts_are_materialized(tmp_path):
    manifest = generate_fixture(tmp_path, seed=5, size="small", storage_mode="sqlite")
    assert manifest["counts"]["tickets"] == 100
    assert manifest["counts"]["sessions"] == manifest["dimensions"]["profile_counts"]["sessions"]
    assert manifest["counts"]["ledger_runs"] == manifest["dimensions"]["profile_counts"]["ledger_runs"]
    validate_manifest(manifest)
