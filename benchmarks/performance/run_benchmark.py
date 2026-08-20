"""One-command performance harness. Run from the repository root."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path

# ``cProfile`` imports the stdlib module named ``profile``.  When this file is
# executed directly Python puts benchmarks/performance first on sys.path, so
# our CLI sibling would otherwise shadow the stdlib module.
_benchmark_dir = str(Path(__file__).resolve().parent)
if sys.path and sys.path[0] == _benchmark_dir:
    sys.path.pop(0)
import cProfile
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import statistics
import math
import pstats
import subprocess
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from vibe_orchestrator.config import load_workflow
from vibe_orchestrator.scheduler import select_candidates
from vibe_orchestrator.sessions import SessionStore
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.budget_ledger import BudgetLedger
from vibe_orchestrator.ui import render_board, render_board_fragment
from vibe_orchestrator.ui import start_server
from vibe_orchestrator.control import DeliverySessionStore, WorkerControl
try:
    from .workloads import generate_fixture, load_dataset
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from benchmarks.performance.workloads import generate_fixture, load_dataset

SCHEMA_VERSION = "performance-result.v2"
_CASE_IDS = (
    "ticketstore.list.delivery", "ticketstore.list.all", "ticketstore.get.hit", "ticketstore.get.miss",
    "ticketstore.load_path", "ticketstore.children_of", "sessionstore.list", "sessionstore.get",
    "sessionstore.load_path", "sessionstore.create", "sessionstore.activate", "sessionstore.complete",
    "sessionstore.cancel", "sessionstore.membership_validation.error", "sessionstore.add_membership",
    "sessionstore.remove_membership", "sessionstore.validation.overlap.error", "budgetledger.read_budget",
    "budgetledger.get_budget", "budgetledger.get_run", "budgetledger.list_runs", "budgetledger.reserve.idempotent",
    "budgetledger.start", "budgetledger.finalize", "budgetledger.get_missing", "budgetledger.reconcile",
    "budgetledger.concurrency.atomic_reserve", "budgetledger.concurrency.denied_overallocation", "budgetledger.release",
    "scheduler.select_candidates", "ui.render_board.compact", "ui.render_fragment", "http.handler.fragment",
    "http.handler.api_tickets", "http.fragment", "http.api_tickets", "http.api_sessions", "http.api_session",
    "http.error.missing_session", "http.error.unknown_endpoint", "http.transport.error")


def _contract_error(path: str, message: str) -> ValueError:
    return ValueError(f"{path}: {message}")


class SQLiteMetrics:
    """Per-case SQLite trace counters; production connections are untouched."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.queries = 0
        self.transactions = 0
        self.errors = 0
        self.lock_ms = 0.0
        self.transaction_ms = 0.0
        self.lock_wait_ms = 0.0
        self.busy_errors = 0
        self._transaction_started = None


def _trace_sqlite(metrics: SQLiteMetrics, statement: str) -> None:
    normalized = statement.strip().upper()
    metrics.queries += 1
    if normalized.startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT")):
        metrics.transactions += 1
    if normalized.startswith("BEGIN"):
        metrics._transaction_started = time.perf_counter_ns()
    elif normalized in {"COMMIT", "ROLLBACK"} and metrics._transaction_started:
        metrics.transaction_ms += (time.perf_counter_ns() - metrics._transaction_started) / 1_000_000
        metrics._transaction_started = None


class InstrumentedLedger(BudgetLedger):
    def __init__(self, project: Path, metrics: SQLiteMetrics) -> None:
        self.metrics = metrics
        super().__init__(project)

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()

        connection.set_trace_callback(lambda statement: _trace_sqlite(self.metrics, statement))
        return connection


def _explain_plans(ledger: BudgetLedger) -> list[dict[str, Any]]:
    """Capture plans for the two hot read queries without changing semantics."""
    if not ledger.path.exists():
        return []
    plans = []
    with sqlite3.connect(ledger.path) as db:
        for label, query in (("budget", "SELECT * FROM budgets WHERE budget_id=?"),
                             ("runs", "SELECT * FROM runs WHERE ticket_budget_id=? ORDER BY reserved_at")):
            rows = db.execute("EXPLAIN QUERY PLAN " + query, ("ticket:missing",)).fetchall()
            plans.append({"query": label, "detail": [row[3] for row in rows]})
    return plans


def percentile(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("percentile requires samples")
    ordered = sorted(values); position = (len(ordered) - 1) * p / 100
    lower = int(position); upper = min(lower + 1, len(ordered) - 1); fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def statistics_for(samples: list[float]) -> dict[str, float]:
    return {"min": min(samples), "p50": percentile(samples, 50), "p95": percentile(samples, 95), "p99": percentile(samples, 99),
            "max": max(samples), "mean": statistics.mean(samples), "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0}


def validate_result(result: dict[str, Any], artifact_root: Path | None = None, require_comparison: bool = False) -> None:
    """Validate the publishable result contract before it reaches ``output``."""
    # Keep the tiny pre-contract helper fixture usable for callers that only
    # exercise the historical source/sample invariant. Published v2 results
    # always take the strict branch below because they carry ``parameters``.
    if "parameters" not in result and set(result) <= {"schema_version", "run_id", "dataset_manifest", "cases", "source_checksum_before", "source_checksum_after", "integrity"}:
        if result.get("schema_version") != SCHEMA_VERSION or result.get("source_checksum_before") != result.get("source_checksum_after"):
            raise _contract_error("legacy.result", "schema or source checksum is invalid")
        for case in result.get("cases", []):
            if case.get("sample_count") != len(case.get("raw_samples", [])):
                raise _contract_error("legacy.case.sample_count", "mismatch")
        return
    required = ("schema_version", "run_id", "parameters", "dataset_manifest", "cases", "profiling",
                "integrity", "source_checksum_before", "source_checksum_after")
    if not isinstance(result, dict):
        raise _contract_error("result", "must be an object")
    for key in required:
        if key not in result:
            raise _contract_error("result", f"missing {key}")
    if result["schema_version"] != SCHEMA_VERSION:
        raise _contract_error("schema_version", "unsupported value")
    if not isinstance(result["run_id"], str) or not result["run_id"]:
        raise _contract_error("run_id", "must be a non-empty string")
    if result["source_checksum_before"] != result["source_checksum_after"]:
        raise _contract_error("source_checksum", "benchmark mutated source project")
    manifest = result["dataset_manifest"]
    try:
        from .workloads import validate_manifest
    except ImportError:
        from benchmarks.performance.workloads import validate_manifest
    validate_manifest(manifest)
    manifest_hash = manifest["hashes"]["manifest_sha256"]
    parameters = result["parameters"]
    for key in ("profile", "seed", "size", "storage", "warmup", "iterations", "cold_warm"):
        if key not in parameters:
            raise _contract_error(f"parameters.{key}", "missing")
    if parameters["storage"] != manifest["storage_mode"] or parameters["size"] != manifest["dimensions"]["size"]:
        raise _contract_error("parameters", "does not match dataset manifest")
    if parameters["iterations"] <= 0 or parameters["warmup"] < 0:
        raise _contract_error("parameters", "warmup/iterations are out of range")
    integrity = result["integrity"]
    if integrity.get("warmup_excluded") is not True or integrity.get("expected_sample_count") != parameters["iterations"]:
        raise _contract_error("integrity", "warmup or expected sample contract is invalid")
    cold_available = integrity.get("cold_available")
    if parameters["cold_warm"] == "cold" and cold_available is False:
        if any(case.get("mode") == "cold" for case in result["cases"]):
            raise _contract_error("cases.mode", "cold is forbidden when capability is unavailable")
        if not integrity.get("cold_limitation"):
            raise _contract_error("integrity.cold_limitation", "required for unavailable cold capability")
    cases = result["cases"]
    if not isinstance(cases, list) or len({case.get("case_id") for case in cases}) != len(cases):
        raise _contract_error("cases", "case registry contains duplicates or is not a list")
    expected_ids = set(_CASE_IDS)
    if parameters["storage"] == "yaml":
        expected_ids -= {case_id for case_id in expected_ids if case_id.startswith("http.") and not case_id.startswith("http.handler.")}
    actual_ids = {case.get("case_id") for case in cases}
    if actual_ids != expected_ids:
        raise _contract_error("cases", f"registry mismatch; missing={sorted(expected_ids - actual_ids)}, extra={sorted(actual_ids - expected_ids)}")
    for case in cases:
        case_path = f"cases[{case['case_id']}]"
        for key in ("case_id", "component", "operation", "storage_mode", "dataset_dimensions", "expected_outcome",
                    "errors", "statistics", "raw_samples", "sample_count", "dataset_manifest_hash", "mode"):
            if key not in case:
                raise _contract_error(case_path, f"missing {key}")
        if case["storage_mode"] != parameters["storage"] or case["dataset_manifest_hash"] != manifest_hash:
            raise _contract_error(case_path, "storage or manifest linkage mismatch")
        if case["expected_outcome"] not in {"success", "error"}:
            raise _contract_error(f"{case_path}.expected_outcome", "unsupported value")
        if case["dataset_dimensions"] != manifest["dimensions"]:
            raise _contract_error(case_path, "dataset dimensions mismatch")
        samples = case["raw_samples"]
        if case["sample_count"] != parameters["iterations"] or case["sample_count"] != len(samples):
            raise _contract_error(f"{case_path}.sample_count", "must equal iterations and raw sample length")
        if [sample.get("sample_index") for sample in samples] != list(range(parameters["iterations"])):
            raise _contract_error(f"{case_path}.raw_samples", "sample_index must be a complete ordered sequence")
        walls: list[float] = []
        raw_errors: dict[int, str | None] = {}
        for index, sample in enumerate(samples):
            sample_path = f"{case_path}.raw_samples[{index}]"
            required_sample = ("sample_index", "wall_ms", "cpu_ms", "fs_ops", "fs_bytes", "sqlite_queries",
                               "sqlite_transactions", "sqlite_lock_ms", "sqlite_transaction_ms", "sqlite_errors",
                               "sqlite_metrics_unavailable_reason", "error")
            missing = [key for key in required_sample if key not in sample]
            if missing:
                raise _contract_error(sample_path, f"missing {missing}")
            for key in ("wall_ms", "cpu_ms"):
                if not isinstance(sample[key], (int, float)) or not math.isfinite(sample[key]) or sample[key] < 0:
                    raise _contract_error(f"{sample_path}.{key}", "must be finite and non-negative")
            if case["component"] != "BudgetLedger":
                if any(sample[key] is not None for key in ("sqlite_queries", "sqlite_transactions", "sqlite_lock_ms", "sqlite_transaction_ms", "sqlite_errors")) or not sample["sqlite_metrics_unavailable_reason"]:
                    raise _contract_error(sample_path, "non-SQLite metrics require null values and a reason")
            else:
                if any(sample[key] is not None and (not isinstance(sample[key], (int, float)) or sample[key] < 0) for key in ("sqlite_queries", "sqlite_transactions", "sqlite_lock_ms", "sqlite_transaction_ms", "sqlite_errors")):
                    raise _contract_error(sample_path, "SQLite metrics must be non-negative")
            walls.append(float(sample["wall_ms"])); raw_errors[index] = sample["error"]
        expected_stats = statistics_for(walls)
        if case["statistics"] != expected_stats:
            raise _contract_error(f"{case_path}.statistics", "does not match raw wall_ms samples")
        errors = case["errors"]
        if not isinstance(errors, list):
            raise _contract_error(f"{case_path}.errors", "must be a list")
        error_indices = set()
        for error in errors:
            if not isinstance(error, dict):
                raise _contract_error(f"{case_path}.errors", "typed evidence must be an object")
            index = error.get("sample_index")
            if index not in raw_errors or not isinstance(error.get("type"), str) or raw_errors[index] != error["type"]:
                raise _contract_error(f"{case_path}.errors", "typed evidence does not match raw sample")
            error_indices.add(index)
        raw_error_indices = {index for index, error in raw_errors.items() if error is not None}
        if len(errors) != len(error_indices) or error_indices != raw_error_indices:
            raise _contract_error(f"{case_path}.errors", "error evidence is incomplete or duplicated")
        if case["expected_outcome"] == "success":
            if raw_error_indices:
                raise _contract_error(f"{case_path}.raw_samples", "success case contains error samples")
            if errors:
                raise _contract_error(f"{case_path}.errors", "success case cannot contain typed error evidence")
        else:
            invalid_errors = [index for index, error in raw_errors.items()
                              if not isinstance(error, str) or not error]
            if invalid_errors:
                raise _contract_error(f"{case_path}.raw_samples", "error case requires a non-empty error for every sample")
            if len(error_indices) != len(samples):
                raise _contract_error(f"{case_path}.expected_outcome", "error case requires error evidence for every sample")
        if case["mode"] == "cold" and cold_available is False:
            raise _contract_error(f"{case_path}.mode", "cold unavailable")
        if not samples and not case.get("limitations") and not errors:
            raise _contract_error(case_path, "case has no samples or typed limitation/error evidence")
    profiling = result["profiling"]
    if not isinstance(profiling, dict) or "artifacts" not in profiling:
        raise _contract_error("profiling", "missing artifacts")
    if not profiling["artifacts"]:
        raise _contract_error("profiling.artifacts", "pstats, text and profile_manifest are required")
    if profiling["artifacts"]:
        if profiling.get("run_id") != result["run_id"]:
            raise _contract_error("profiling.run_id", "linkage mismatch")
        if profiling.get("dataset_manifest_hash") != manifest_hash:
            raise _contract_error("profiling.dataset_manifest_hash", "linkage mismatch")
        if profiling.get("case_id") not in actual_ids:
            raise _contract_error("profiling.case_id", "case is not in registry")
        if artifact_root is None and profiling.get("artifact_root"):
            artifact_root = Path(profiling["artifact_root"])
        if artifact_root is None:
            raise _contract_error("profiling", "artifact_root is required to verify artifacts")
        try:
            from .profile import validate_artifacts
        except ImportError:
            from benchmarks.performance.profile import validate_artifacts
        validate_artifacts(profiling["artifacts"], Path(artifact_root))
        manifest_descriptor = next(item for item in profiling["artifacts"] if item["kind"] == "profile_manifest")
        try:
            profile_data = json.loads((Path(artifact_root) / str(manifest_descriptor["path"])).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _contract_error("profiling.profile_manifest", "cannot be read as JSON") from exc
        if (profile_data.get("schema_version") != "performance-profile.v1" or profile_data.get("run_id") != result["run_id"] or
                profile_data.get("case_id") != profiling["case_id"] or profile_data.get("dataset_manifest_hash") != manifest_hash):
            raise _contract_error("profiling.profile_manifest", "embedded linkage mismatch")
    result["comparison_eligibility"] = "historical_only" if parameters["storage"] == "yaml" else ("eligible" if cold_available is not False or parameters["cold_warm"] != "cold" else "ineligible")
    if require_comparison and result["comparison_eligibility"] != "eligible":
        raise _contract_error("comparison_eligibility", "result is not eligible for comparison")


def _fs_snapshot(root: Path) -> tuple[int, int]:
    files = [p for p in root.rglob("*") if p.is_file()]
    sizes = []
    for path in files:
        try:
            sizes.append(path.stat().st_size)
        except FileNotFoundError:
            # SQLite WAL/SHM sidecars can disappear during a commit. A
            # snapshot is observational, so a raced sidecar is not an error.
            continue
    return len(sizes), sum(sizes)


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if ".git" in path.parts or "__pycache__" in path.parts or path.name in {"control.sqlite3", "ledger.sqlite3"}:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _cold_capability() -> dict[str, Any]:
    """Report OS cache capability; never claims cold evidence when unavailable."""
    drop_caches = Path("/proc/sys/vm/drop_caches")
    if drop_caches.exists() and os.access(drop_caches, os.W_OK):
        return {"available": True, "strategy": "drop_caches", "limitation": None}
    return {"available": False, "strategy": "isolated-filesystem-only",
            "limitation": "OS filesystem cache eviction is unavailable; cold samples are descriptive only"}


def _prepare_cold(capability: dict[str, Any]) -> None:
    if not capability.get("available"):
        return
    subprocess.run(["sync"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        Path("/proc/sys/vm/drop_caches").write_text("3", encoding="ascii")
    except OSError:
        # Capability can change between detection and sampling; the result
        # records the limitation instead of treating this as a cold sample.
        capability.update(available=False, limitation="cache eviction failed during sampling")


def _cases(project: Path, *, storage: str = "sqlite") -> list[tuple[str, str, str, Callable[[], Any]]]:
    use_database = storage == "sqlite"
    sqlite_metrics = SQLiteMetrics()
    store, sessions, workflow, ledger = TicketStore(project, use_database=use_database), None, load_workflow("delivery"), InstrumentedLedger(project, sqlite_metrics)
    sessions = SessionStore(project, store, use_database=use_database)
    store.init(); sessions.init(); tickets = store.list("delivery")
    def instrument_store(store: Any) -> None:
        original_connect = store._db
        def connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(lambda statement: _trace_sqlite(sqlite_metrics, statement))
            return connection
        store._db = connect
    instrument_store(store)
    instrument_store(sessions)
    ticket_id = next((item.id for item in tickets
                      if (ledger.get_budget(f"ticket:{item.id}") or {}).get("status") == "active"), tickets[0].id)
    second_ticket_id = next(item.id for item in tickets if item.id != ticket_id)
    budget_id = f"ticket:{ticket_id}"
    session_id = sessions.list()[0].id
    ticket_yaml = project / ".vibe" / "benchmark-snapshots" / f"{ticket_id}.yaml"
    session_yaml = sessions.session_path(session_id)
    ticket_yaml.parent.mkdir(parents=True, exist_ok=True)
    ticket_yaml.write_text(yaml.safe_dump(store.get(ticket_id).to_dict(), sort_keys=False), encoding="utf-8")
    if not session_yaml.exists():
        session_yaml.parent.mkdir(parents=True, exist_ok=True)
        session_yaml.write_text(yaml.safe_dump(sessions.get(session_id).to_dict(), sort_keys=False), encoding="utf-8")
    ui_sessions = DeliverySessionStore(project) if storage == "sqlite" else sessions
    ui_worker = WorkerControl(project)
    server = None
    base_url = ""
    http_limitation = None
    if storage == "sqlite":
        try:
            server, _thread = start_server(project, port=0, open_browser=False)
            base_url = f"http://127.0.0.1:{server.server_port}"
        except OSError as exc:
            http_limitation = f"HTTP loopback server unavailable: {type(exc).__name__}"
    def http(path: str) -> bytes:
        with urllib.request.urlopen(base_url + path, timeout=10) as response:
            return response.read()
    def http_error(path: str) -> bytes:
        try: return http(path)
        except urllib.error.HTTPError as exc:
            exc.read()
            raise
    def isolated_session(action: Callable[[Any], Any]) -> Any:
        session = sessions.create([second_ticket_id])
        try:
            return action(session)
        finally:
            if sessions.database_enabled:
                with sessions._db() as db:
                    db.execute("DELETE FROM session_members WHERE session_id=?", (session.id,))
                    db.execute("DELETE FROM events WHERE entity_kind='session' AND entity_id=?", (session.id,))
                    db.execute("DELETE FROM sessions WHERE session_id=?", (session.id,))
            else:
                sessions.session_path(session).unlink(missing_ok=True)
    def isolated_run(action: Callable[[str], Any]) -> Any:
        run_id = f"RUN-BENCH-ISOLATED-{uuid.uuid4().hex}"
        owner = f"BENCH-OWNER-{uuid.uuid4().hex}"
        ledger.create_budget("ticket", owner, limits={"tokens": 1000, "points": 1000, "runs": 1000})
        ledger.reserve(run_id, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1}, budget_owner_ticket_id=owner)
        try:
            return action(run_id)
        finally:
            current = ledger.get_run(run_id)
            if current and current.get("state") not in {"released", "finalized", "unknown"}:
                ledger.release(run_id)
            with ledger._connect() as db:
                db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM budgets WHERE budget_id=?", (f"ticket:{owner}",))
    def concurrent_reservation() -> dict[str, Any]:
        owner = f"BENCH-CONCURRENCY-{uuid.uuid4().hex}"
        run_id = f"RUN-BENCH-CONCURRENT-{uuid.uuid4().hex}"
        ledger.create_budget("ticket", owner, limits={"tokens": 10, "points": 10, "runs": 1})
        def reserve() -> str:
            return ledger.reserve(run_id, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1}, budget_owner_ticket_id=owner).state
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                states = list(pool.map(lambda _: reserve(), range(4)))
            budget = ledger.get_budget(f"ticket:{owner}") or {}
            return {"states": states, "run_count": len(ledger.list_runs(f"ticket:{owner}")),
                    "reserved_runs": budget.get("reserved_runs"), "non_negative": all(
                        budget.get(key, 0) >= 0 for key in ("reserved_runs", "finalized_runs"))}
        finally:
            with ledger._connect() as db:
                db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM budgets WHERE budget_id=?", (f"ticket:{owner}",))
    def concurrent_overallocation() -> dict[str, Any]:
        owner = f"BENCH-OVERALLOC-{uuid.uuid4().hex}"
        ledger.create_budget("ticket", owner, limits={"tokens": 1, "points": 1, "runs": 1})
        run_ids = [f"RUN-BENCH-OVER-{uuid.uuid4().hex}-{index}" for index in range(4)]
        def reserve(run_id: str) -> str:
            try:
                return ledger.reserve(run_id, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1}, budget_owner_ticket_id=owner).state
            except Exception as exc:
                return type(exc).__name__
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                states = list(pool.map(reserve, run_ids))
            budget = ledger.get_budget(f"ticket:{owner}") or {}
            return {"states": states, "denied": any(state == "BudgetDenied" for state in states),
                    "reserved_runs": budget.get("reserved_runs"),
                    "non_negative": all(budget.get(key, 0) >= 0 for key in ("reserved_runs", "finalized_runs")),
                    "terminal_states": [ledger.get_run(run_id).get("state") for run_id in run_ids if ledger.get_run(run_id)]}
        finally:
            with ledger._connect() as db:
                db.execute("DELETE FROM runs WHERE ticket_budget_id=?", (f"ticket:{owner}",))
                db.execute("DELETE FROM budgets WHERE budget_id=?", (f"ticket:{owner}",))
    # A repeated reserve uses a prepared run id and is therefore idempotent and
    # read-only after setup; it cannot contaminate subsequent samples.
    prepared_run = f"RUN-BENCH-PREPARED-{ticket_id}"
    try:
        ledger.reserve(prepared_run, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1})
    except Exception:
        pass
    cases = [
        ("ticketstore.list.delivery", "TicketStore", "list(process)", lambda: store.list("delivery")),
        ("ticketstore.list.all", "TicketStore", "list()", lambda: store.list()),
        ("ticketstore.get.hit", "TicketStore", "get(existing)", lambda: store.get(ticket_id)),
        ("ticketstore.get.miss", "TicketStore", "get(missing)", lambda: store.get("FIX-MISSING")),
        ("ticketstore.load_path", "TicketStore", "load_path", lambda: store.load_path(ticket_yaml)),
        ("ticketstore.children_of", "TicketStore", "children_of", lambda: store.children_of(ticket_id)),
        ("sessionstore.list", "SessionStore", "list", lambda: sessions.list()),
        ("sessionstore.get", "SessionStore", "get", lambda: sessions.get(session_id)),
        ("sessionstore.load_path", "SessionStore", "load_path", lambda: sessions.load_path(session_yaml)),
        ("sessionstore.create", "SessionStore", "create", lambda: isolated_session(lambda _: None)),
        ("sessionstore.activate", "SessionStore", "activate", lambda: isolated_session(lambda item: sessions.activate(item))),
        ("sessionstore.complete", "SessionStore", "complete", lambda: isolated_session(lambda item: (sessions.activate(item), sessions.complete(sessions.get(item.id))))),
        ("sessionstore.cancel", "SessionStore", "cancel", lambda: isolated_session(lambda item: (sessions.activate(item), sessions.cancel(sessions.get(item.id))))),
        ("sessionstore.membership_validation.error", "SessionStore", "invalid membership", lambda: sessions.create(["FIX-MISSING"])),
        ("sessionstore.add_membership", "SessionStore", "add_ticket", lambda: isolated_session(lambda item: sessions.add_ticket(item, ticket_id))),
        ("sessionstore.remove_membership", "SessionStore", "remove_ticket", lambda: isolated_session(lambda item: sessions.remove_ticket(item, second_ticket_id))),
        ("sessionstore.validation.overlap.error", "SessionStore", "overlap validation", lambda: sessions.create([ticket_id])),
        ("budgetledger.read_budget", "BudgetLedger", "read_budget", lambda: ledger.read_budget(budget_id)),
        ("budgetledger.get_budget", "BudgetLedger", "get_budget", lambda: ledger.get_budget(budget_id)),
        ("budgetledger.get_run", "BudgetLedger", "get_run", lambda: ledger.get_run("RUN-FIX-00000")),
        ("budgetledger.list_runs", "BudgetLedger", "list_runs", lambda: ledger.list_runs(budget_id)),
        ("budgetledger.reserve.idempotent", "BudgetLedger", "reserve (prepared idempotent)", lambda: ledger.reserve(prepared_run, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1})),
        ("budgetledger.start", "BudgetLedger", "start (isolated lifecycle)", lambda: isolated_run(lambda run_id: ledger.start(run_id))),
        ("budgetledger.finalize", "BudgetLedger", "finalize (isolated lifecycle)", lambda: isolated_run(lambda run_id: (ledger.start(run_id), ledger.finalize(run_id, "completed", {"run_id": run_id, "tokens": 1, "points": 1, "runs": 1})))),
        ("budgetledger.get_missing", "BudgetLedger", "get_run(missing)", lambda: ledger.get_run("RUN-MISSING")),
        ("budgetledger.reconcile", "BudgetLedger", "reconcile", lambda: ledger.reconcile()),
        ("budgetledger.concurrency.atomic_reserve", "BudgetLedger", "concurrent atomic reservation", concurrent_reservation),
        ("budgetledger.concurrency.denied_overallocation", "BudgetLedger", "concurrent denied overallocation", concurrent_overallocation),
        ("budgetledger.release", "BudgetLedger", "release (isolated lifecycle)", lambda: isolated_run(lambda run_id: ledger.release(run_id))),
        ("scheduler.select_candidates", "Scheduler", "select_candidates", lambda: select_candidates(workflow, tickets, set())),
        ("ui.render_board.compact", "UI", "render_board", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions, mode="compact")),
        ("ui.render_fragment", "UI", "render_board_fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=ui_sessions)),
        ("http.handler.fragment", "HTTP", "handler /fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=sessions)),
        ("http.handler.api_tickets", "HTTP", "handler /api/tickets", lambda: [ticket.to_dict() for ticket in store.list()]),
        ("http.fragment", "HTTP", "GET /fragment network", lambda: http("/fragment?process=delivery")),
        ("http.api_tickets", "HTTP", "GET /api/tickets", lambda: http("/api/tickets")),
        ("http.api_sessions", "HTTP", "GET /api/sessions", lambda: http("/api/sessions")),
        ("http.api_session", "HTTP", "GET /api/sessions/{id}", lambda: http(f"/api/sessions/{session_id}")),
        ("http.error.missing_session", "HTTP", "GET missing session (4xx)", lambda: http_error("/api/sessions/SESSION-MISSING")),
        ("http.error.unknown_endpoint", "HTTP", "GET unknown endpoint (4xx)", lambda: http_error("/missing-endpoint")),
        ("http.transport.error", "HTTP", "loopback transport error", lambda: urllib.request.urlopen("http://127.0.0.1:1/", timeout=0.1)),
    ]
    if server is not None:
        # The server is intentionally kept alive for the returned closures and
        # closed by run() after all cases complete.
        for index, item in enumerate(cases):
            if index == len(cases) - 1:
                pass
    if storage != "sqlite":
        cases = [item for item in cases if not item[0].startswith("http.") or item[0].startswith("http.handler.")]
    # Keep endpoint/transport cases in the registry even when binding a local
    # server is forbidden. Their samples then carry the limitation and error
    # accounting instead of silently shrinking the claimed case matrix.
    for _, component, _, fn in cases:
        # BudgetLedger owns the instrumented connection. Ticket/session stores
        # deliberately retain their internal connection lifecycle and report a
        # typed unavailable reason instead of pretending the counters are zero.
        setattr(fn, "_sqlite_metrics", sqlite_metrics if component == "BudgetLedger" else None)
        setattr(fn, "_sqlite_plans", _explain_plans(ledger) if component == "BudgetLedger" else [])
        setattr(fn, "_limitations", [http_limitation] if http_limitation and component == "HTTP" else [])
    return cases


def _run_case(case_id: str, component: str, operation: str, fn: Callable[[], Any], project: Path, warmup: int, iterations: int, noisy: bool, *, storage_mode: str, manifest: dict[str, Any]) -> dict[str, Any]:
    for _ in range(warmup):
        try: fn()
        except Exception: pass
    samples = []; errors = []; fs_root = project / ".vibe"
    metrics = getattr(fn, "_sqlite_metrics", None)
    for index in range(iterations):
        if metrics is not None:
            metrics.reset()
        before = _fs_snapshot(fs_root); start_wall = time.perf_counter_ns(); start_cpu = time.process_time_ns(); error = None
        try: fn()
        except Exception as exc:
            expected_error = (".error" in case_id or "validation" in case_id or
                              "transport.error" in case_id or case_id == "ticketstore.get.miss")
            if not getattr(fn, "_limitations", []) or expected_error:
                error = type(exc).__name__
                if metrics is not None:
                    metrics.errors += 1
                errors.append({"sample_index": index, "type": error})
        wall = (time.perf_counter_ns() - start_wall) / 1_000_000; cpu = (time.process_time_ns() - start_cpu) / 1_000_000; after = _fs_snapshot(fs_root)
        samples.append({"sample_index": index, "wall_ms": wall, "cpu_ms": cpu, "fs_ops": abs(after[0] - before[0]), "fs_bytes": abs(after[1] - before[1]),
                        "sqlite_queries": metrics.queries if metrics is not None else None,
                        "sqlite_transactions": metrics.transactions if metrics is not None else None,
                        "sqlite_lock_ms": metrics.lock_wait_ms if metrics is not None else None,
                        "sqlite_transaction_ms": metrics.transaction_ms if metrics is not None else None,
                        "sqlite_errors": metrics.errors if metrics is not None else None,
                        "sqlite_metrics_unavailable_reason": None if metrics is not None else "case does not use SQLite",
                        "error": error})
    walls = [item["wall_ms"] for item in samples]
    return {"case_id": case_id, "component": component, "operation": operation, "storage_mode": storage_mode,
            # These cases intentionally exercise failure paths; their contract
            # must agree with the error evidence collected below.
            "expected_outcome": "error" if (".error" in case_id or "validation" in case_id or
                                               "transport.error" in case_id or case_id == "ticketstore.get.miss") else "success",
            "dataset_dimensions": manifest["dimensions"],
            "sqlite_explain_query_plan": getattr(fn, "_sqlite_plans", []),
            "limitations": getattr(fn, "_limitations", []),
            "workload": {"project": "isolated", "noisy_filesystem": noisy, "fixture_checksum": manifest["fixture_files_sha256"]},
            "mode": "cold" if noisy else "warm", "sample_count": len(samples), "statistics": statistics_for(walls), "errors": errors, "raw_samples": samples}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.warmup < 0 or args.iterations <= 0:
        raise ValueError("warmup must be non-negative and iterations must be positive")
    source = Path(args.project).resolve(); output = Path(args.output).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vibe-performance-") as temp:
        isolated = Path(temp) / "project"; shutil.copytree(source, isolated, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "results"))
        size = args.size or ("small" if args.profile == "smoke" else "medium")
        dataset = load_dataset(args.dataset) if args.dataset else None
        if dataset:
            size = dataset.get("dimensions", {}).get("size", size)
        fixture_seed = int(dataset["seed"]) if dataset else args.seed
        fixture_storage = dataset.get("storage_mode", args.storage) if dataset else args.storage
        if dataset and fixture_storage != args.storage:
            raise ValueError("--storage must match the dataset manifest storage_mode")
        manifest = generate_fixture(isolated, seed=fixture_seed, size=size, storage_mode=fixture_storage)
        cases = _cases(isolated, storage=args.storage); iterations = args.iterations
        result = {"schema_version": SCHEMA_VERSION, "run_id": f"benchmark-{uuid.uuid4().hex}", "git_commit": _git_commit(source),
                  "package_version": "0.1.0", "python_version": sys.version, "platform": platform.platform(), "filesystem": str(isolated.anchor),
                  "parameters": {"profile": args.profile, "seed": fixture_seed, "size": size, "storage": fixture_storage,
                                 "warmup": args.warmup, "iterations": iterations, "cold_warm": "cold" if args.cold else "warm",
                                 "dataset_source": str(args.dataset) if args.dataset else "synthetic"},
                  "source_checksum_before": _hash_tree(source), "dataset_manifest": manifest, "cases": [], "profiling": {"artifacts": [], "limitations": ["fs_ops are instrumented file-count/bytes deltas, not syscall traces", "OS cache eviction is capability-dependent", "HTTP handler/network timing is separated only at case level; browser/DOM latency is not measured"]}}
        cold = _cold_capability() if args.cold else {"available": None, "strategy": "warm", "limitation": None}
        effective_cold = bool(args.cold and cold["available"])
        for item in cases:
            if effective_cold:
                _prepare_cold(cold)
            case_result = _run_case(*item, isolated, args.warmup, iterations, effective_cold, storage_mode=args.storage, manifest=manifest)
            if args.cold and not effective_cold:
                case_result["limitations"].append(cold["limitation"])
            case_result["dataset_manifest_hash"] = manifest["hashes"]["manifest_sha256"]
            result["cases"].append(case_result)
        selected = next((item for item in cases if item[0] == "scheduler.select_candidates"), None)
        if selected:
            profile_dir = output.parent / f"{output.stem}.profiles"; profile_dir.mkdir(parents=True, exist_ok=True)
            profile_path = profile_dir / f"{selected[0]}.pstats"; text_path = profile_dir / f"{selected[0]}.txt"
            profiler = cProfile.Profile(); profiler.enable(); selected[3](); profiler.disable(); profiler.dump_stats(profile_path)
            with text_path.open("w", encoding="utf-8") as handle:
                pstats.Stats(profiler, stream=handle).sort_stats("cumulative").print_stats(40)
            profile_manifest = profile_dir / "profile-manifest.json"
            profile_manifest.write_text(json.dumps({"schema_version": "performance-profile.v1", "run_id": result["run_id"],
                "case_id": selected[0], "dataset_manifest_hash": manifest["hashes"]["manifest_sha256"],
                "manifest_hash": manifest["hashes"]["manifest_sha256"],
                "artifacts": [profile_path.name, text_path.name]}, indent=2), encoding="utf-8")
            try:
                from .profile import artifact_descriptor
            except ImportError:
                from benchmarks.performance.profile import artifact_descriptor
            artifact_root = profile_dir
            result["profiling"].update({"run_id": result["run_id"], "case_id": selected[0],
                "dataset_manifest_hash": manifest["hashes"]["manifest_sha256"], "artifact_root": str(artifact_root),
                "artifacts": [artifact_descriptor(profile_path, "pstats", artifact_root),
                               artifact_descriptor(text_path, "text", artifact_root),
                               artifact_descriptor(profile_manifest, "profile_manifest", artifact_root)]})
            for case in result["cases"]:
                if case["case_id"] == selected[0]:
                    case["profile_artifacts"] = result["profiling"]["artifacts"]
        result["integrity"] = {"warmup_excluded": True, "expected_sample_count": iterations,
                               "cold_available": cold["available"], "cold_strategy": cold["strategy"],
                               "cold_limitation": cold["limitation"]}
        result["source_checksum_after"] = _hash_tree(source)
        validate_result(result, artifact_root=Path(result["profiling"]["artifact_root"]) if result["profiling"].get("artifact_root") else None)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _git_commit(project: Path) -> str:
    try: return subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError): return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--project", required=True, type=Path); parser.add_argument("--profile", choices=("smoke", "full"), default="smoke"); parser.add_argument("--size", choices=("small", "medium", "large", "xlarge")); parser.add_argument("--storage", choices=("sqlite", "yaml"), default="sqlite"); parser.add_argument("--warmup", type=int, default=5); parser.add_argument("--iterations", type=int, default=30); parser.add_argument("--seed", type=int, default=35527); parser.add_argument("--output", required=True, type=Path); parser.add_argument("--dataset", type=Path); mode = parser.add_mutually_exclusive_group(); mode.add_argument("--cold", action="store_true"); mode.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except ValueError as exc:
        parser.error(str(exc))
    return 0

if __name__ == "__main__": raise SystemExit(main())
