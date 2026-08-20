"""One-command performance harness. Run from the repository root."""
from __future__ import annotations

import argparse
import atexit
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
import pstats
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

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
    from .workloads import generate_fixture, load_dataset, materialize_dataset
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from benchmarks.performance.workloads import generate_fixture, load_dataset, materialize_dataset

SCHEMA_VERSION = "performance-result.v2"
DEFAULT_SEED = 35527


def parse_seed(value: str) -> int:
    """Accept decimal seeds and ticket-style hexadecimal suffixes."""
    try:
        return int(value, 10)
    except ValueError:
        normalized = value.strip().upper()
        if normalized.startswith("0X"):
            normalized = normalized[2:]
        if normalized and all(char in "0123456789ABCDEF" for char in normalized):
            return int(normalized, 16)
        raise argparse.ArgumentTypeError(
            "seed must be a decimal integer or hexadecimal ticket suffix"
        )

# Kept here as the single registry used by the benchmark and standalone profiler.
REQUIRED_PROFILE_CASES = {
    "TicketStore": "ticketstore.list.delivery",
    "SessionStore": "sessionstore.list",
    "BudgetLedger": "budgetledger.list_runs",
    "Orchestrator": "orchestrator.scan_sort_cycle",
    "Scheduler": "scheduler.select_candidates",
    "UI": "ui.render_board.compact",
    "HTTP": "http.api_tickets",
}

PROFILE_DESCRIPTOR_FIELDS = {"run_id", "case_id", "component", "manifest_hash", "path",
                             "kind", "sha256", "size_bytes", "command_hash"}


@dataclass
class CaseSpec:
    """Named benchmark case.  ``__getitem__`` keeps the v1 tuple adapter."""

    case_id: str
    component: str
    operation: str
    run: Callable[[], Any]
    kind: str = "read_only"
    storage_modes: tuple[str, ...] = ("sqlite", "yaml")
    expected_outcome: str = "success"
    setup: Callable[[], Any] | None = None
    teardown: Callable[[], Any] | None = None
    limitations: list[str] = field(default_factory=list)

    def __getitem__(self, index: int) -> Any:
        # Existing callers used (case_id, component, operation, callable).
        return (self.case_id, self.component, self.operation, self.run)[index]


class CaseRegistry(list[CaseSpec]):
    """List-compatible registry with explicit resource ownership."""

    def __init__(self, cases: Iterator[CaseSpec], cleanup: Callable[[], None] | None = None):
        super().__init__(cases)
        self.cleanup = cleanup or (lambda: None)


_VOLATILE_FIELDS = {"created_at", "updated_at", "started_at", "completed_at", "cancelled_at",
                    "reserved_at", "started_at", "terminal_at", "timestamp", "consumed_at"}


def _normalize_logical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_logical(item) for key, item in sorted(value.items())
                if key not in _VOLATILE_FIELDS and not key.endswith("_at")}
    if isinstance(value, list):
        normalized = [_normalize_logical(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


def _logical_snapshot(project: Path) -> dict[str, Any]:
    """Capture control-plane entities and files, excluding only volatile times."""
    root = project / ".vibe"
    entities: dict[str, Any] = {}
    for database in sorted(root.rglob("*.sqlite3")) if root.exists() else []:
        try:
            with sqlite3.connect(database) as db:
                db.row_factory = sqlite3.Row
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                entities[str(database.relative_to(root))] = {
                    table: _normalize_logical([dict(row) for row in db.execute(f'SELECT * FROM "{table}"')])
                    for table in sorted(tables)
                }
        except sqlite3.Error:
            continue
    files = {}
    yaml_cache = getattr(_logical_snapshot, "_yaml_cache", {})
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".yaml", ".yml", ".json"} and path.name not in {"manifest.json"}:
                try:
                    key = str(path)
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    cached = yaml_cache.get(key)
                    if cached and cached[0] == signature:
                        loaded = cached[1]
                    else:
                        loaded = _normalize_logical(yaml.safe_load(path.read_text(encoding="utf-8")))
                        yaml_cache[key] = (signature, loaded)
                    files[str(path.relative_to(root))] = loaded
                except (OSError, yaml.YAMLError):
                    files[str(path.relative_to(root))] = path.read_bytes().hex()
    _logical_snapshot._yaml_cache = yaml_cache
    payload = _normalize_logical({"entities": entities, "files": files})
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return {"hash": hashlib.sha256(encoded).hexdigest(), "entities": payload["entities"],
            "paths": sorted(files)}


class SQLiteMetrics:
    """Per-case SQLite trace counters; production connections are untouched."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._lock = getattr(self, "_lock", threading.Lock())
        self.queries = 0
        self.transactions = 0
        self.errors = 0
        self.busy_errors = 0
        self.transaction_ms = 0.0
        self.lock_wait_ms = None
        self.lock_wait_count = None
        self._transaction_started = None
        self.attribution = {"source": "sqlite3.trace_callback+connection_factory",
                            "contract_version": "sqlite-attribution.v1",
                            "lock_wait": "unavailable: sqlite3 exposes no portable busy handler"}

    def record_error(self, error: sqlite3.Error) -> None:
        text = str(error).lower()
        with self._lock:
            self.errors += 1
            if "locked" in text or "busy" in text:
                self.busy_errors += 1


class InstrumentedConnection(sqlite3.Connection):
    """Connection-level attribution without changing SQLite timeout semantics."""

    def __init__(self, *args: Any, metrics: SQLiteMetrics, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._metrics = metrics

    def _execute(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(super(), method)(*args, **kwargs)
        except sqlite3.Error as exc:
            self._metrics.record_error(exc)
            raise

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._execute("execute", *args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._execute("executemany", *args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._execute("executescript", *args, **kwargs)


def instrumented_connection_factory(metrics: SQLiteMetrics) -> Callable[..., sqlite3.Connection]:
    def trace(statement: str) -> None:
        normalized = statement.strip().upper()
        now = time.perf_counter_ns()
        with metrics._lock:
            metrics.queries += 1
            if normalized.startswith("BEGIN"):
                metrics.transactions += 1
                metrics._transaction_started = now
            elif normalized.startswith(("COMMIT", "ROLLBACK")) and metrics._transaction_started is not None:
                metrics.transaction_ms += (now - metrics._transaction_started) / 1_000_000
                metrics._transaction_started = None

    def factory(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = lambda *args, **inner: InstrumentedConnection(*args, metrics=metrics, **inner)
        connection = sqlite3.connect(path, **kwargs)
        connection.set_trace_callback(trace)
        return connection
    return factory


class InstrumentedLedger(BudgetLedger):
    def __init__(self, project: Path, metrics: SQLiteMetrics) -> None:
        self.metrics = metrics
        super().__init__(project, connection_factory=instrumented_connection_factory(metrics))

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()

        def trace(statement: str) -> None:
            normalized = statement.strip().upper()
            now = time.perf_counter_ns()
            with self.metrics._lock:
                self.metrics.queries += 1
                if normalized.startswith("BEGIN"):
                    self.metrics.transactions += 1
                    self.metrics._transaction_started = now
                elif normalized.startswith(("COMMIT", "ROLLBACK")) and self.metrics._transaction_started is not None:
                    self.metrics.transaction_ms += (now - self.metrics._transaction_started) / 1_000_000
                    self.metrics._transaction_started = None

        connection.set_trace_callback(trace)
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


def validate_result(result: dict[str, Any]) -> None:
    """Check result integrity invariants used by CI and reviewers."""
    for key in ("schema_version", "run_id", "dataset_manifest", "cases", "source_checksum_before", "source_checksum_after"):
        if key not in result:
            raise ValueError(f"result missing {key}")
    provenance = result.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("result missing provenance")
    for key in ("source_kind", "synthetic_only", "seed", "manifest_hash"):
        if key not in provenance:
            raise ValueError(f"provenance missing {key}")
    if provenance["source_kind"] not in {"synthetic", "approved_dataset"}:
        raise ValueError("invalid provenance source_kind")
    if provenance["synthetic_only"] is not (provenance["source_kind"] == "synthetic"):
        raise ValueError("provenance synthetic_only marker is inconsistent")
    if not isinstance(provenance["seed"], int) or not isinstance(provenance["manifest_hash"], str):
        raise ValueError("invalid provenance seed or manifest hash")
    if result["source_checksum_before"] != result["source_checksum_after"]:
        raise ValueError("benchmark mutated source project")
    profiling = result.get("profiling")
    if profiling is not None:
        artifacts = profiling.get("artifacts", [])
        if artifacts:
            if not profiling.get("run_id") or profiling.get("run_id") != result["run_id"]:
                raise ValueError("profiling run_id is not linked to result")
            if profiling.get("manifest_hash") != provenance["manifest_hash"]:
                raise ValueError("profiling manifest hash does not match result")
            for descriptor in artifacts:
                if set(descriptor) != PROFILE_DESCRIPTOR_FIELDS:
                    raise ValueError("profiling descriptor has an invalid schema")
                if descriptor["run_id"] != profiling["run_id"] or descriptor["manifest_hash"] != profiling["manifest_hash"]:
                    raise ValueError("profiling descriptor provenance mismatch")
                path = Path(str(descriptor["path"]))
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("profiling artifact path must be repository-relative")
                if descriptor["kind"] not in {"pstats", "text", "profile_manifest"}:
                    raise ValueError("invalid profiling artifact kind")
    for case in result["cases"]:
        for key in ("case_id", "component", "operation", "storage_mode", "dataset_dimensions", "expected_outcome", "errors", "statistics", "raw_samples"):
            if key not in case:
                raise ValueError(f"case missing {key}")
        kind = case.get("kind", "read_only")
        if kind not in {"read_only", "mutation"}:
            raise ValueError(f"unknown case kind for {case.get('case_id')}")
        if kind == "mutation" and "isolation" not in case:
            raise ValueError(f"mutation case missing isolation for {case.get('case_id')}")
        if case["sample_count"] != len(case["raw_samples"]):
            raise ValueError(f"sample count mismatch for {case.get('case_id')}")
        for sample in case["raw_samples"]:
            if sample.get("sample_index", -1) < 0 or "wall_ms" not in sample or "error" not in sample:
                raise ValueError("invalid sample schema")
            if sample.get("sqlite_queries") is not None:
                required_metrics = ("sqlite_transactions", "sqlite_errors", "sqlite_busy_errors",
                                     "sqlite_transaction_ms", "sqlite_lock_wait_ms", "sqlite_lock_wait_count",
                                     "sqlite_attribution")
                if any(field not in sample for field in required_metrics):
                    raise ValueError("SQLite sample missing attribution field")
                attribution = sample["sqlite_attribution"]
                if not isinstance(attribution, dict) or not attribution.get("source"):
                    raise ValueError("invalid SQLite attribution")
                if sample["sqlite_lock_wait_ms"] is not None and sample["sqlite_lock_wait_count"] is None:
                    raise ValueError("lock wait duration requires lock wait count")
                if not isinstance(sample["sqlite_transaction_ms"], (int, float)) or sample["sqlite_transaction_ms"] < 0:
                    raise ValueError("transaction duration must be a non-negative number")
        sample_indices = {sample["sample_index"] for sample in case["raw_samples"]}
        if sample_indices and sample_indices != set(range(case["sample_count"])):
            raise ValueError("sample indices must form a contiguous range from zero")
        if any(error.get("sample_index") not in sample_indices for error in case["errors"]):
            raise ValueError("error references an absent sample")
        if "isolation" in case:
            for key in ("before_hash", "after_hash", "leaked_entities", "leaked_paths", "cleanup_errors", "clean"):
                if key not in case["isolation"]:
                    raise ValueError(f"isolation missing {key} for {case.get('case_id')}")
            if kind == "mutation" and case["isolation"]["clean"] is not True:
                raise ValueError(f"unclean mutation case {case.get('case_id')}")


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
        if ".git" in path.parts or path.name in {"control.sqlite3", "ledger.sqlite3"}:
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
    connection_factory = instrumented_connection_factory(sqlite_metrics) if use_database else None
    store, sessions, workflow, ledger = TicketStore(project, use_database=use_database, connection_factory=connection_factory), None, load_workflow("delivery"), InstrumentedLedger(project, sqlite_metrics)
    sessions = SessionStore(project, store, use_database=use_database, connection_factory=connection_factory)
    store.init(); sessions.init(); tickets = store.list("delivery")
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
    base_url = None
    http_limitation = None if storage == "sqlite" else "HTTP loopback cases are unavailable in YAML registry mode"
    if storage == "sqlite":
        try:
            server, _thread = start_server(project, port=0, open_browser=False)
            # ``start_server`` accepts port 0.  Read the address assigned by
            # the socket rather than reconstructing it from the requested
            # port; the latter produces ``http://127.0.0.1:0`` and can hide
            # an unavailable endpoint behind a malformed relative URL.
            base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        except OSError as exc:
            http_limitation = f"HTTP loopback server unavailable: {type(exc).__name__}"
    def http(path: str) -> bytes:
        if base_url is None:
            raise RuntimeError(http_limitation or "HTTP loopback server unavailable")
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
        ("budgetledger.reserve.idempotent", "BudgetLedger", "reserve (isolated idempotent)", lambda: isolated_run(lambda run_id: (ledger.reserve(run_id, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1}), ledger.reserve(run_id, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1})))),
        ("budgetledger.start", "BudgetLedger", "start (isolated lifecycle)", lambda: isolated_run(lambda run_id: ledger.start(run_id))),
        ("budgetledger.finalize", "BudgetLedger", "finalize (isolated lifecycle)", lambda: isolated_run(lambda run_id: (ledger.start(run_id), ledger.finalize(run_id, "completed", {"run_id": run_id, "tokens": 1, "points": 1, "runs": 1})))),
        ("budgetledger.get_missing", "BudgetLedger", "get_run(missing)", lambda: ledger.get_run("RUN-MISSING")),
        ("budgetledger.reconcile", "BudgetLedger", "reconcile", lambda: ledger.reconcile()),
        ("budgetledger.concurrency.atomic_reserve", "BudgetLedger", "concurrent atomic reservation", concurrent_reservation),
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
    def isolated_budget(action: Callable[[str, str], Any]) -> Any:
        owner = f"BENCH-BUDGET-{uuid.uuid4().hex}"
        ledger.create_budget("ticket", owner, limits={"tokens": 1000, "points": 1000, "runs": 1000})
        try:
            return action(owner, f"ticket:{owner}")
        finally:
            with ledger._connect() as db:
                db.execute("DELETE FROM reconciliation_facts WHERE run_id IN (SELECT run_id FROM runs WHERE ticket_budget_id=?)", (f"ticket:{owner}",))
                db.execute("DELETE FROM adjustments WHERE adjusts_run_id IN (SELECT run_id FROM runs WHERE ticket_budget_id=?)", (f"ticket:{owner}",))
                db.execute("DELETE FROM runs WHERE ticket_budget_id=?", (f"ticket:{owner}",))
                db.execute("DELETE FROM budget_decisions WHERE target_id IN (?, ?)", (owner, f"ticket:{owner}"))
                db.execute("DELETE FROM budgets WHERE budget_id=?", (f"ticket:{owner}",))

    def isolated_ticket(action: Callable[[Any], Any]) -> Any:
        item = store.create("delivery", "task", "benchmark synthetic")
        try:
            return action(item)
        finally:
            if store.database_enabled:
                with store._db() as db:
                    db.execute("DELETE FROM events WHERE entity_kind='ticket' AND entity_id=?", (item.id,))
                    db.execute("DELETE FROM tickets WHERE ticket_id=?", (item.id,))
            else:
                store.ticket_path(item).unlink(missing_ok=True)

    def limited_mutation(label: str) -> Callable[[], Any]:
        def invoke() -> None:
            raise RuntimeError(f"{label} requires a network/UI actor context")
        setattr(invoke, "_limitations", ["handler mutation requires an external request context"])
        return invoke

    # Add the public operation matrix even where a capability-specific operation
    # is expected to reject its synthetic input. Such a case is still useful:
    # the rejection and its clean teardown are measured and reported explicitly.
    cases.extend([
        ("ticketstore.save", "TicketStore", "save", lambda: isolated_ticket(lambda item: store.save(item))),
        ("ticketstore.create", "TicketStore", "create", lambda: isolated_ticket(lambda item: item)),
        ("ticketstore.record_run_event", "TicketStore", "record_run_event", lambda: isolated_ticket(lambda item: store.record_run_event(item.id, "benchmark", {"run_id": "BENCH"}))),
        ("ticketstore.is_done", "TicketStore", "is_done", lambda: store.is_done(store.get(ticket_id))),
        ("ticketstore.run_path", "TicketStore", "run_path", lambda: store.run_path("RUN-FIX-00000")),
        ("sessionstore.effective_ticket_ids", "SessionStore", "effective_ticket_ids", lambda: sessions.effective_ticket_ids(sessions.get(session_id))),
        ("sessionstore.participants", "SessionStore", "participants", lambda: sessions.get(session_id).participants),
        ("sessionstore.inherit_ticket", "SessionStore", "inherit_ticket", lambda: isolated_session(lambda item: sessions.inherit_ticket(item, second_ticket_id, source_ticket=ticket_id))),
        ("sessionstore.override_ticket", "SessionStore", "override_ticket", lambda: isolated_session(lambda item: sessions.override_ticket(item, ticket_id, actor="benchmark", reason="coverage"))),
        ("sessionstore.agent_add_ticket", "SessionStore", "agent_add_ticket", lambda: isolated_session(lambda item: sessions.agent_add_ticket(item.id, ticket_id, actor="benchmark", origin="benchmark"))),
        ("sessionstore.agent_remove_ticket", "SessionStore", "agent_remove_ticket", lambda: isolated_session(lambda item: sessions.agent_remove_ticket(item.id, second_ticket_id, actor="benchmark", origin="benchmark"))),
        ("sessionstore.agent_update_membership", "SessionStore", "agent_update_membership", lambda: isolated_session(lambda item: sessions.agent_update_membership(item.id, [{"ticket_id": second_ticket_id, "priority": 10}], actor="benchmark", origin="benchmark"))),
        ("budgetledger.create_budget", "BudgetLedger", "create_budget", lambda: isolated_budget(lambda owner, budget_id: ledger.get_budget(budget_id))),
        ("budgetledger.set_status", "BudgetLedger", "set_status", lambda: isolated_budget(lambda owner, _: ledger.set_status(f"ticket:{owner}", "active"))),
        ("budgetledger.reserve", "BudgetLedger", "reserve", lambda: isolated_run(lambda run_id: ledger.reserve(run_id, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1}))),
        ("budgetledger.increase_limit", "BudgetLedger", "increase_limit", lambda: ledger.increase_limit(actor="benchmark", target_scope="ticket", target_id=budget_id, dimension="tokens", delta=1, reason="coverage", reference="BENCH", one_shot=True)),
        ("budgetledger.allow_overrun", "BudgetLedger", "allow_overrun", lambda: ledger.allow_overrun(actor="benchmark", target_scope="ticket", target_id=budget_id, dimensions=["tokens"], reason="coverage", reference="BENCH", one_shot=True)),
        ("budgetledger.resolve_unknown", "BudgetLedger", "resolve_unknown", lambda: ledger.resolve_unknown(actor="benchmark", run_id="RUN-MISSING", reason="coverage", reference="BENCH", one_shot=True)),
        ("budgetledger.adjustment", "BudgetLedger", "adjustment", lambda: isolated_run(lambda run_id: ledger.adjustment(run_id, {"tokens": 0, "points": 0, "runs": 0}, reason="coverage", author="benchmark"))),
        ("budgetledger.list_decisions", "BudgetLedger", "list_decisions", lambda: ledger.list_decisions()),
        ("budgetledger.list_reconciliation_facts", "BudgetLedger", "list_reconciliation_facts", lambda: ledger.list_reconciliation_facts()),
        ("scheduler.wip_count", "Scheduler", "wip_count", lambda: __import__("vibe_orchestrator.scheduler", fromlist=["wip_count"]).wip_count(tickets, "active")),
        ("ui.render_board", "UI", "render_board", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions)),
        ("ui.render_board_fragment", "UI", "render_board_fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=ui_sessions)),
        ("ui.GET_board", "UI", "GET /", lambda: http("/")),
        ("ui.GET_fragment", "UI", "GET /fragment", lambda: http("/fragment?process=delivery")),
        ("ui.GET_drawer", "UI", "GET /drawer", lambda: http(f"/drawer?ticket={ticket_id}")),
        ("ui.GET_api_tickets", "UI", "GET /api/tickets", lambda: http("/api/tickets")),
        ("ui.GET_api_sessions", "UI", "GET /api/sessions", lambda: http("/api/sessions")),
        ("ui.GET_api_session", "UI", "GET /api/sessions/{id}", lambda: http(f"/api/sessions/{session_id}")),
        ("ui.expected_4xx", "UI", "expected 4xx", lambda: http_error("/api/sessions/SESSION-MISSING")),
        *[(f"ui.{name}", "UI", name, limited_mutation(name)) for name in ("POST_create", "POST_move", "POST_retry", "POST_release_retry", "POST_session_add_remove_activate_complete_cancel", "POST_workers", "PATCH_agent_ticket", "PATCH_agent_session")],
    ])
    # Keep endpoint/transport cases in the registry even when binding a local
    # server is forbidden. Their samples then carry the limitation and error
    # accounting instead of silently shrinking the claimed case matrix.
    specs = []
    for case_id, component, operation, fn in cases:
        # BudgetLedger owns the instrumented connection. Ticket/session stores
        # deliberately retain their internal connection lifecycle and report a
        # typed unavailable reason instead of pretending the counters are zero.
        setattr(fn, "_sqlite_metrics", sqlite_metrics if use_database and component in {"TicketStore", "SessionStore", "BudgetLedger"} else None)
        setattr(fn, "_sqlite_plans", _explain_plans(ledger) if component == "BudgetLedger" else [])
        if http_limitation and component in {"HTTP", "UI"}:
            setattr(fn, "_limitations", list(getattr(fn, "_limitations", [])) + [http_limitation])
        mutation = any(token in case_id for token in (".create", ".save", ".record", ".add", ".remove", ".activate", ".complete", ".cancel", ".inherit", ".override", ".agent_", ".reserve", ".start", ".finalize", ".release", ".set_status", ".increase", ".allow", ".resolve", ".adjustment", "POST_", "PATCH_"))
        expected = "error" if ".error" in case_id or ".miss" in case_id or "validation" in case_id or "expected_4xx" in case_id or "transport" in case_id else "success"
        modes = ("sqlite",) if component in {"HTTP", "UI"} and not case_id.startswith("http.handler") else ("sqlite", "yaml")
        specs.append(CaseSpec(case_id, component, operation, fn, "mutation" if mutation else "read_only", storage_modes=modes, expected_outcome=expected,
                              limitations=list(getattr(fn, "_limitations", []))))
    cleanup = (lambda: (server.shutdown(), server.server_close())) if server is not None else None
    return CaseRegistry(iter(specs), cleanup)


def _run_case(spec: CaseSpec, project: Path, warmup: int, iterations: int, noisy: bool, *, storage_mode: str, manifest: dict[str, Any]) -> dict[str, Any]:
    case_id, component, operation, fn = spec.case_id, spec.component, spec.operation, spec.run
    for _ in range(warmup):
        try: fn()
        except Exception: pass
    samples = []; errors = []; cleanup_errors: list[dict[str, Any]] = []; fs_root = project / ".vibe"; before_snapshot = _logical_snapshot(project)
    track_sample_isolation = spec.kind == "mutation"
    metrics = getattr(fn, "_sqlite_metrics", None)
    for index in range(iterations):
        if metrics is not None:
            metrics.reset()
        before = _fs_snapshot(fs_root) if track_sample_isolation else (0, 0)
        sample_snapshot = _logical_snapshot(project) if track_sample_isolation else None
        start_wall = time.perf_counter_ns(); start_cpu = time.process_time_ns(); error = None
        try:
            if spec.setup:
                spec.setup()
            fn()
        except Exception as exc:
            error = type(exc).__name__
            errors.append({"sample_index": index, "type": error})
        finally:
            if spec.teardown:
                try:
                    spec.teardown()
                except Exception as exc:
                    cleanup_errors.append({"sample_index": index, "type": type(exc).__name__})
        wall = (time.perf_counter_ns() - start_wall) / 1_000_000; cpu = (time.process_time_ns() - start_cpu) / 1_000_000
        after = _fs_snapshot(fs_root) if track_sample_isolation else (0, 0)
        after_snapshot = _logical_snapshot(project) if track_sample_isolation else None
        samples.append({"sample_index": index, "wall_ms": wall, "cpu_ms": cpu, "fs_ops": abs(after[0] - before[0]), "fs_bytes": abs(after[1] - before[1]),
                        "sqlite_queries": metrics.queries if metrics is not None else None,
                        "sqlite_transactions": metrics.transactions if metrics is not None else None,
                        "sqlite_transaction_ms": metrics.transaction_ms if metrics is not None else None,
                        "sqlite_lock_wait_ms": metrics.lock_wait_ms if metrics is not None else None,
                        "sqlite_lock_wait_count": metrics.lock_wait_count if metrics is not None else None,
                        "sqlite_errors": metrics.errors if metrics is not None else None,
                        "sqlite_busy_errors": metrics.busy_errors if metrics is not None else None,
                        "sqlite_attribution": metrics.attribution if metrics is not None else {"source": None, "limitation": "case does not use SQLite"},
                        "sqlite_metrics_unavailable_reason": None if metrics is not None else "case does not use SQLite",
                        "error": error, "isolation_clean": after_snapshot["hash"] == sample_snapshot["hash"] if track_sample_isolation else True})
    walls = [item["wall_ms"] for item in samples]
    after_run = _logical_snapshot(project)
    isolation = {"before_hash": before_snapshot["hash"], "after_hash": after_run["hash"],
                 "leaked_entities": [] if before_snapshot["hash"] == after_run["hash"] else ["logical_snapshot_changed"],
                 "leaked_paths": sorted(set(after_run["paths"]) - set(before_snapshot["paths"])),
                 "cleanup_errors": cleanup_errors, "clean": before_snapshot["hash"] == after_run["hash"] and not cleanup_errors}
    return {"case_id": case_id, "component": component, "operation": operation, "kind": spec.kind, "storage_mode": storage_mode,
            "expected_outcome": spec.expected_outcome, "storage_modes": list(spec.storage_modes),
            "dataset_dimensions": manifest["dimensions"],
            "sqlite_explain_query_plan": getattr(fn, "_sqlite_plans", []),
            "limitations": sorted(set(spec.limitations + getattr(fn, "_limitations", []))),
            "workload": {"project": "isolated", "noisy_filesystem": noisy, "fixture_checksum": manifest["fixture_files_sha256"]},
            "mode": "cold" if noisy else "warm", "sample_count": len(samples), "statistics": statistics_for(walls), "errors": errors, "raw_samples": samples,
            "isolation": isolation}


def _unavailable_case(spec: CaseSpec, *, storage_mode: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Keep a registry case visible when its storage mode is not applicable."""
    return {
        "case_id": spec.case_id, "component": spec.component, "operation": spec.operation,
        "kind": spec.kind, "storage_mode": storage_mode,
        "storage_modes": list(spec.storage_modes), "dataset_dimensions": manifest["dimensions"],
        "expected_outcome": spec.expected_outcome, "errors": [], "raw_samples": [],
        "sample_count": 0, "statistics": {},
        "limitations": [f"case is unavailable for storage mode {storage_mode}"],
        "unavailable": True,
        "isolation": {"before_hash": None, "after_hash": None, "leaked_entities": [],
                      "leaked_paths": [], "cleanup_errors": [], "clean": True},
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.warmup < 0 or args.iterations <= 0:
        raise ValueError("warmup must be non-negative and iterations must be positive")
    source = Path(args.project).resolve(); output = Path(args.output).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vibe-performance-") as temp:
        isolated = Path(temp) / "project"; shutil.copytree(source, isolated, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "results"))
        size = args.size or ("small" if args.profile == "smoke" else "medium")
        dataset = load_dataset(args.dataset / "manifest.json" if args.dataset and args.dataset.is_dir() else args.dataset) if args.dataset else None
        if dataset:
            size = dataset.get("dimensions", {}).get("size", size)
        fixture_seed = int(dataset["seed"]) if dataset else args.seed
        fixture_storage = dataset.get("storage_mode", args.storage) if dataset else args.storage
        if dataset and fixture_storage != args.storage:
            raise ValueError("--storage must match the dataset manifest storage_mode")
        iterations = args.iterations
        storage_modes = (args.storage, "yaml" if args.storage == "sqlite" else "sqlite")
        passes: dict[str, tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = {}
        for storage_mode in storage_modes:
            pass_project = isolated if storage_mode == args.storage else Path(temp) / f"project-{storage_mode}"
            if pass_project != isolated:
                shutil.copytree(source, pass_project, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "results"))
            if dataset:
                manifest = materialize_dataset(pass_project, args.dataset, dataset, storage_mode=storage_mode)
            else:
                manifest = generate_fixture(pass_project, seed=fixture_seed, size=size, storage_mode=storage_mode)
            cases = _cases(pass_project, storage=storage_mode)
            atexit.register(cases.cleanup)
            pass_cases = []
            cold = _cold_capability() if args.cold else {"available": None, "strategy": "warm", "limitation": None}
            effective_cold = bool(args.cold and cold["available"])
            for item in cases:
                if storage_mode not in item.storage_modes:
                    pass_cases.append(_unavailable_case(item, storage_mode=storage_mode, manifest=manifest))
                    continue
                if effective_cold:
                    _prepare_cold(cold)
                case_result = _run_case(item, pass_project, args.warmup, iterations, effective_cold,
                                        storage_mode=storage_mode, manifest=manifest)
                if args.cold and not effective_cold:
                    case_result["limitations"].append(cold["limitation"])
                case_result["dataset_manifest_hash"] = manifest["hashes"]["manifest_sha256"]
                pass_cases.append(case_result)
            passes[storage_mode] = (manifest, pass_cases, {"available": True, "limitation": cold["limitation"]})
            cases.cleanup()
        manifest, primary_cases, _ = passes[args.storage]
        alternate_storage = storage_modes[1]
        alternate_manifest, alternate_cases, alternate_state = passes[alternate_storage]
        primary_logical = _normalize_logical(manifest.get("logical", {}))
        alternate_logical = _normalize_logical(alternate_manifest.get("logical", {}))
        primary_logical.pop("storage_mode", None); alternate_logical.pop("storage_mode", None)
        comparison = {"baseline_storage": args.storage, "alternate_storage": alternate_storage,
                      "baseline_manifest_hash": manifest["hashes"]["manifest_sha256"],
                      "alternate_manifest_hash": alternate_manifest["hashes"]["manifest_sha256"],
                      "logical_equivalent": primary_logical == alternate_logical,
                      "readback_equivalent": manifest["readback"].get("digest") == alternate_manifest["readback"].get("digest"),
                      "cases": {}, "limitations": []}
        if not comparison["logical_equivalent"] or not comparison["readback_equivalent"]:
            comparison["limitations"].append("storage conversion/read-back is not logically equivalent")
        alternate_by_id = {case["case_id"]: case for case in alternate_cases}
        for case in primary_cases:
            other = alternate_by_id.get(case["case_id"])
            comparison["cases"][case["case_id"]] = {"baseline": case["storage_mode"],
                "alternate": other["storage_mode"] if other else None,
                "alternate_available": other is not None,
                "limitation": None if other else "case is not registered for alternate storage"}
        manifest_hash = manifest["hashes"]["manifest_sha256"]
        result = {"schema_version": SCHEMA_VERSION, "run_id": args.run_id or f"benchmark-{uuid.uuid4().hex}", "git_commit": _git_commit(source),
                  "package_version": "0.1.0", "python_version": sys.version, "platform": platform.platform(), "filesystem": str(isolated.anchor),
                  "parameters": {"profile": args.profile, "seed": fixture_seed, "size": size, "storage": fixture_storage,
                                 "warmup": args.warmup, "iterations": iterations, "cold_warm": "cold" if args.cold else "warm",
                                 "dataset_source": str(args.dataset) if args.dataset else "synthetic"},
                  "source_checksum_before": _hash_tree(source), "dataset_manifest": manifest, "cases": primary_cases,
                  "provenance": {"source_kind": manifest["source_kind"],
                                 "synthetic_only": manifest["source_kind"] == "synthetic",
                                 "seed": fixture_seed, "manifest_hash": manifest_hash,
                                 "dataset_manifest_hash": manifest_hash,
                                 "dataset_tree_sha256": manifest["dataset_tree_sha256"],
                                 "readback_digest": manifest["readback"]["digest"],
                                 "command": list(sys.argv)},
                  "storage_comparison": comparison, "alternate_run": {"storage_mode": alternate_storage,
                      "manifest": alternate_manifest, "cases": alternate_cases, "available": alternate_state["available"]},
                  "profiling": {"artifacts": [], "limitations": ["fs_ops are instrumented file-count/bytes deltas, not syscall traces", "OS cache eviction is capability-dependent", "HTTP handler/network timing is separated only at case level; browser/DOM latency is not measured"]}}
        if args.profile_artifacts:
            profile_root = Path(args.profile_artifacts).resolve()
            profile_manifest_path = profile_root / "profile-manifest.json"
            profile_manifest = json.loads(profile_manifest_path.read_text(encoding="utf-8"))
            if profile_manifest.get("run_id") != result["run_id"]:
                raise ValueError("profile run_id must equal benchmark run_id")
            if profile_manifest.get("manifest_hash") != manifest_hash:
                raise ValueError("profile manifest hash does not match benchmark")
            descriptors = []
            for item in profile_manifest.get("artifacts", []):
                descriptor = dict(item)
                path = Path(str(descriptor.get("path", "")))
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("profile artifact path must be relative")
                actual = profile_root / path
                data = actual.read_bytes()
                if descriptor.get("kind") != "profile_manifest" and (descriptor.get("sha256") != hashlib.sha256(data).hexdigest() or descriptor.get("size_bytes") != len(data)):
                    raise ValueError(f"profile artifact checksum mismatch: {path}")
                # profile-manifest.json contains its own descriptor, so its
                # checksum is necessarily recursive. Store the final digest
                # in the result descriptor after reading the completed file.
                if descriptor.get("kind") == "profile_manifest":
                    descriptor["sha256"] = hashlib.sha256(data).hexdigest()
                    descriptor["size_bytes"] = len(data)
                descriptors.append(descriptor)
            result["profiling"].update({"run_id": result["run_id"], "manifest_hash": manifest_hash,
                                        "profile_manifest": "profile-manifest.json", "artifacts": descriptors,
                                        "coverage": profile_manifest.get("coverage", [])})
        result["integrity"] = {"warmup_excluded": True, "expected_sample_count": iterations,
                               "cold_available": None, "cold_strategy": "per-storage-pass",
                               "cold_limitation": "cold capability is recorded per storage pass"}
        result["source_checksum_after"] = _hash_tree(source)
        validate_result(result)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        cases.cleanup()
    return result


def _git_commit(project: Path) -> str:
    try: return subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError): return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--project", required=True, type=Path); parser.add_argument("--profile", choices=("smoke", "full"), default="smoke"); parser.add_argument("--size", choices=("small", "medium", "large", "xlarge")); parser.add_argument("--storage", choices=("sqlite", "yaml"), default="sqlite"); parser.add_argument("--warmup", type=int, default=5); parser.add_argument("--iterations", type=int, default=30); parser.add_argument("--seed", type=parse_seed, default=DEFAULT_SEED); parser.add_argument("--run-id"); parser.add_argument("--profile-artifacts", type=Path); parser.add_argument("--output", required=True, type=Path); parser.add_argument("--dataset", type=Path); mode = parser.add_mutually_exclusive_group(); mode.add_argument("--cold", action="store_true"); mode.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except ValueError as exc:
        parser.error(str(exc))
    return 0

if __name__ == "__main__": raise SystemExit(main())
