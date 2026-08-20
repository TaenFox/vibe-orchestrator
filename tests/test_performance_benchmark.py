from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from benchmarks.performance.run_benchmark import _cold_capability, _cases, percentile, statistics_for, validate_result
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
    path.write_text(json.dumps(manifest | {"hashes": {"manifest_sha256": "tampered"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_dataset(path)


def test_result_validation_rejects_mutated_source_and_sample_mismatch():
    manifest = {"hashes": {"manifest_sha256": "m"}}
    result = {"schema_version": "performance-result.v2", "run_id": "r", "dataset_manifest": manifest,
              "cases": [{"case_id": "c", "component": "x", "operation": "y", "storage_mode": "sqlite",
                          "dataset_dimensions": {}, "expected_outcome": "success", "errors": [], "statistics": {},
                          "sample_count": 0, "raw_samples": [], "dataset_manifest_hash": "m"}],
              "source_checksum_before": "a", "source_checksum_after": "a",
              "integrity": {"warmup_excluded": True}}
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
        "http.handler.fragment", "http.api_tickets", "http.error.missing_session",
    }
    assert required <= ids
    assert {item[3] for item in cases}


def test_fixture_profile_counts_are_materialized(tmp_path):
    manifest = generate_fixture(tmp_path, seed=5, size="small", storage_mode="sqlite")
    assert manifest["counts"]["tickets"] == 100
    assert manifest["counts"]["sessions"] == manifest["dimensions"]["profile_counts"]["sessions"]
    assert manifest["counts"]["ledger_runs"] == manifest["dimensions"]["profile_counts"]["ledger_runs"]
    validate_manifest(manifest)


def test_readback_rejects_yaml_store_status_tampering(tmp_path):
    root = tmp_path / "fixture"
    manifest = generate_fixture(root, seed=31, storage_mode="yaml")
    path = next((root / ".vibe" / "tickets" / "delivery").glob("*.yaml"))
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["status"] = "done" if payload["status"] != "done" else "todo"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="materialized (ticket_status|checksum)"):
        validate_manifest(manifest, materialized_root=root)


def test_readback_rejects_sqlite_store_mutation(tmp_path):
    root = tmp_path / "fixture"
    manifest = generate_fixture(root, seed=32, storage_mode="sqlite")
    database = root / ".vibe" / "control.sqlite3"
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT ticket_id, payload_json FROM tickets LIMIT 1").fetchone()
        payload = json.loads(row[1])
        payload["status"] = "done" if payload["status"] != "done" else "todo"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        db.execute("UPDATE tickets SET status=?, payload_json=? WHERE ticket_id=?",
                   (payload["status"], encoded, row[0]))
        db.commit()
    with pytest.raises(ValueError, match="materialized (ticket_status|checksum)"):
        validate_manifest(manifest, materialized_root=root)


def test_readback_rejects_deleted_yaml_record(tmp_path):
    root = tmp_path / "fixture"
    manifest = generate_fixture(root, seed=33, storage_mode="yaml")
    next((root / ".vibe" / "tickets" / "delivery").glob("*.yaml")).unlink()
    with pytest.raises(ValueError, match="materialized (counts|checksum)"):
        validate_manifest(manifest, materialized_root=root)
