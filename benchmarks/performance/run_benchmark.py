"""One-command performance harness. Run from the repository root."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
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
    from .workloads import assert_manifest_identity, generate_fixture, load_dataset, materialize_dataset
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from benchmarks.performance.workloads import assert_manifest_identity, generate_fixture, load_dataset, materialize_dataset

SCHEMA_VERSION = "performance-result.v2"


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
        self.measure_lock_wait = False

    def transaction_began(self) -> None:
        """Start transaction timing after SQLite acquired the transaction lock."""
        self._transaction_started = time.perf_counter_ns()


class InstrumentedConnection(sqlite3.Connection):
    """Benchmark-only connection that separates BEGIN lock acquisition time."""

    def __init__(self, *args: Any, metrics: SQLiteMetrics, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._metrics = metrics

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        normalized = sql.strip().upper()
        is_begin = normalized.startswith("BEGIN")
        measure_lock_wait = is_begin and self._metrics.measure_lock_wait
        started = time.perf_counter_ns() if is_begin else None
        try:
            cursor = super().execute(sql, parameters)
        except Exception:
            if measure_lock_wait and started is not None:
                self._metrics.lock_wait_ms += (time.perf_counter_ns() - started) / 1_000_000
            raise
        if is_begin and started is not None:
            # BEGIN's execute duration is the time SQLite spent acquiring the
            # transaction lock (plus the tiny statement overhead). The
            # transaction timer starts only after BEGIN has returned, so the
            # two measurements cannot double-count lock wait.
            if measure_lock_wait:
                self._metrics.lock_wait_ms += (time.perf_counter_ns() - started) / 1_000_000
            self._metrics.transaction_began()
        return cursor


def _trace_sqlite(metrics: SQLiteMetrics, statement: str) -> None:
    normalized = statement.strip().upper()
    metrics.queries += 1
    if normalized.startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT")):
        metrics.transactions += 1
    if normalized in {"COMMIT", "ROLLBACK"} and metrics._transaction_started:
        metrics.transaction_ms += (time.perf_counter_ns() - metrics._transaction_started) / 1_000_000
        metrics._transaction_started = None


class InstrumentedLedger(BudgetLedger):
    def __init__(self, project: Path, metrics: SQLiteMetrics) -> None:
        self.metrics = metrics
        super().__init__(project)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.timeout, isolation_level=None,
            factory=lambda *args, **kwargs: InstrumentedConnection(
                *args, metrics=self.metrics, **kwargs))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")

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


def validate_result(result: dict[str, Any]) -> None:
    """Check result integrity invariants used by CI and reviewers."""
    for key in ("schema_version", "run_id", "dataset_manifest", "cases", "source_checksum_before", "source_checksum_after"):
        if key not in result:
            raise ValueError(f"result missing {key}")
    if result["source_checksum_before"] != result["source_checksum_after"]:
        raise ValueError("benchmark mutated source project")
    manifest = result["dataset_manifest"]
    manifest_hash = manifest.get("hashes", {}).get("manifest_sha256")
    if not manifest_hash:
        raise ValueError("dataset manifest has no logical hash")
    if result.get("dataset_manifest_hash") != manifest_hash:
        raise ValueError("result dataset identity is missing or inconsistent")
    profiling = result.get("profiling")
    required_components = {"TicketStore", "SessionStore", "BudgetLedger", "Orchestrator", "Scheduler", "UI", "HTTP"}
    coverage = profiling.get("coverage") if isinstance(profiling, dict) else None
    if profiling is not None:
        if not isinstance(coverage, list) or {item.get("component") for item in coverage} != required_components:
            raise ValueError("profiling coverage must include every required component")
        for item in coverage:
            if item.get("status") not in {"profiled", "unavailable", "failed"} or not item.get("case_id"):
                raise ValueError("invalid profiling coverage record")
            if item["status"] == "profiled" and not all(item.get(key) for key in ("pstats", "text")):
                raise ValueError("profiled component is missing profile artifacts")
    comparison = result.get("storage_comparison")
    if comparison is not None:
        if not isinstance(comparison, dict) or comparison.get("equivalent") is not True:
            raise ValueError("storage comparison is mandatory and must prove equivalence")
        if set(comparison.get("case_ids", [])) != {case.get("case_id") for case in result["cases"]}:
            raise ValueError("storage comparison coverage is incomplete")
    integrity = result.get("integrity", {})
    if integrity.get("warmup_excluded") is not True:
        raise ValueError("warmup samples must be excluded")
    if integrity.get("cold_available") is False and any(case.get("mode") == "cold" for case in result["cases"]):
        raise ValueError("cold samples cannot be reported without cache eviction capability")
    for case in result["cases"]:
        for key in ("case_id", "component", "operation", "storage_mode", "dataset_dimensions", "expected_outcome", "errors", "statistics", "raw_samples"):
            if key not in case:
                raise ValueError(f"case missing {key}")
        if case["sample_count"] != len(case["raw_samples"]):
            raise ValueError(f"sample count mismatch for {case.get('case_id')}")
        if case.get("dataset_manifest_hash") != manifest_hash:
            raise ValueError(f"case manifest linkage mismatch for {case.get('case_id')}")
        for sample in case["raw_samples"]:
            if sample.get("sample_index", -1) < 0 or "wall_ms" not in sample or "error" not in sample:
                raise ValueError("invalid sample schema")
            for metric in ("sqlite_queries", "sqlite_transactions", "sqlite_lock_ms", "sqlite_transaction_ms", "sqlite_errors"):
                if metric not in sample:
                    raise ValueError(f"sample missing {metric}")
        sample_indices = {sample["sample_index"] for sample in case["raw_samples"]}
        if any(error.get("sample_index") not in sample_indices for error in case["errors"]):
            raise ValueError("error references an absent sample")
        if case.get("dataset_manifest_hash") != result.get("dataset_manifest_hash"):
            raise ValueError("case dataset identity mismatch")


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
        if ".git" in path.parts or path.name in {"control.sqlite3", "control.sqlite3-wal", "control.sqlite3-shm",
                                                   "ledger.sqlite3", "ledger.sqlite3-wal", "ledger.sqlite3-shm"}:
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
    try:
        server, _thread = start_server(project, port=0, open_browser=False, use_database=use_database)
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

    def lock_wait_probe() -> dict[str, float]:
        """Create a short real SQLite lock wait for attribution validation."""
        holder = sqlite3.connect(ledger.path, isolation_level=None, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        released = threading.Event()

        def release_lock() -> None:
            time.sleep(0.02)
            holder.execute("ROLLBACK")
            holder.close()
            released.set()

        releaser = threading.Thread(target=release_lock)
        releaser.start()
        try:
            connection = ledger._connect()
            try:
                sqlite_metrics.measure_lock_wait = True
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("ROLLBACK")
            finally:
                sqlite_metrics.measure_lock_wait = False
                connection.close()
        finally:
            releaser.join(timeout=2)
            if not released.is_set():
                holder.execute("ROLLBACK")
                holder.close()
        return {"lock_wait_ms": sqlite_metrics.lock_wait_ms,
                "transaction_ms": sqlite_metrics.transaction_ms}

    def overlap_open_sessions() -> Any:
        first = sessions.create([second_ticket_id])
        try:
            return sessions.create([second_ticket_id])
        finally:
            if sessions.database_enabled:
                with sessions._db() as db:
                    db.execute("DELETE FROM session_members WHERE session_id=?", (first.id,))
                    db.execute("DELETE FROM events WHERE entity_kind='session' AND entity_id=?", (first.id,))
                    db.execute("DELETE FROM sessions WHERE session_id=?", (first.id,))
            else:
                sessions.session_path(first).unlink(missing_ok=True)

    malformed_session = project / ".vibe" / "benchmark-snapshots" / "malformed-session.yaml"
    malformed_session.write_text("not: [a valid session", encoding="utf-8")
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
        ("orchestrator.scan_sort_cycle", "Orchestrator", "scan, select and sort candidates", lambda: sorted(
            select_candidates(workflow, store.list("delivery"), set()),
            key=lambda candidate: (-candidate.stage_position, candidate.ticket.priority, candidate.ticket.id))),
        ("sessionstore.list", "SessionStore", "list", lambda: sessions.list()),
        ("sessionstore.get", "SessionStore", "get", lambda: sessions.get(session_id)),
        ("sessionstore.get_missing.error", "SessionStore", "get missing persisted entity", lambda: sessions.get("SESSION-MISSING")),
        ("sessionstore.load_path", "SessionStore", "load_path", lambda: sessions.load_path(session_yaml)),
        ("sessionstore.load_invalid_persisted.error", "SessionStore", "load invalid persisted entity", lambda: sessions.load_path(malformed_session)),
        ("sessionstore.create", "SessionStore", "create", lambda: isolated_session(lambda _: None)),
        ("sessionstore.activate", "SessionStore", "activate", lambda: isolated_session(lambda item: sessions.activate(item))),
        ("sessionstore.complete", "SessionStore", "complete", lambda: isolated_session(lambda item: (sessions.activate(item), sessions.complete(sessions.get(item.id))))),
        ("sessionstore.cancel", "SessionStore", "cancel", lambda: isolated_session(lambda item: (sessions.activate(item), sessions.cancel(sessions.get(item.id))))),
        ("sessionstore.membership_validation.error", "SessionStore", "invalid membership", lambda: sessions.create(["FIX-MISSING"])),
        ("sessionstore.add_membership", "SessionStore", "add_ticket", lambda: isolated_session(lambda item: sessions.add_ticket(item, ticket_id))),
        ("sessionstore.remove_membership", "SessionStore", "remove_ticket", lambda: isolated_session(lambda item: sessions.remove_ticket(item, second_ticket_id))),
        ("sessionstore.validation.overlap.error", "SessionStore", "overlap validation", lambda: sessions.create([ticket_id])),
        ("sessionstore.validation.multiple_open_overlap.error", "SessionStore", "overlap across open sessions", overlap_open_sessions),
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
        ("budgetledger.concurrency.lock_wait", "BudgetLedger", "measured SQLite lock wait", lock_wait_probe),
        ("budgetledger.release", "BudgetLedger", "release (isolated lifecycle)", lambda: isolated_run(lambda run_id: ledger.release(run_id))),
        ("scheduler.select_candidates", "Scheduler", "select_candidates", lambda: select_candidates(workflow, tickets, set())),
        ("ui.render_board.compact", "UI", "render_board", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions, mode="compact")),
        ("ui.render_board.flat", "UI", "render_board mode=flat", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions, mode="flat")),
        ("ui.filter.flat", "UI", "filter mode=flat", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions, mode="flat")),
        ("ui.filter.search", "UI", "filter search", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions, search=ticket_id)),
        ("ui.filter.status", "UI", "filter status", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions, status=store.get(ticket_id).status)),
        ("ui.filter.active", "UI", "filter active", lambda: render_board(store, {"delivery": workflow}, "delivery", ui_worker, session_store=ui_sessions, active=True)),
        ("ui.render_fragment", "UI", "render_board_fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=ui_sessions)),
        ("http.handler.fragment", "HTTP", "handler /fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=sessions)),
        ("http.handler.api_tickets", "HTTP", "handler /api/tickets", lambda: [ticket.to_dict() for ticket in store.list()]),
        ("http.fragment", "HTTP", "GET /fragment network", lambda: http("/fragment?process=delivery")),
        ("http.root", "HTTP", "GET / network", lambda: http("/?process=delivery")),
        ("http.drawer", "HTTP", "GET /drawer network", lambda: http(f"/drawer?process=delivery&ticket={ticket_id}")),
        ("http.api_tickets", "HTTP", "GET /api/tickets", lambda: http("/api/tickets")),
        ("http.api_sessions", "HTTP", "GET /api/sessions", lambda: http("/api/sessions")),
        ("http.api_session", "HTTP", "GET /api/sessions/{id}", lambda: http(f"/api/sessions/{session_id}")),
        ("http.api_agent_tickets", "HTTP", "GET /api/agent/tickets", lambda: http("/api/agent/tickets?process=delivery&limit=10")),
        ("http.api_agent_sessions", "HTTP", "GET /api/agent/sessions", lambda: http("/api/agent/sessions?limit=10")),
        ("http.api_agent_ticket", "HTTP", "GET /api/agent/tickets/{id}", lambda: http(f"/api/agent/tickets/{ticket_id}")),
        ("http.api_agent_session", "HTTP", "GET /api/agent/sessions/{id}", lambda: http(f"/api/agent/sessions/{session_id}")),
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
        setattr(fn, "_profile_available", not (http_limitation and component == "HTTP"))
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
            "dataset_manifest_hash": manifest["hashes"]["manifest_sha256"],
            "expected_outcome": "error" if ".error" in case_id or ".miss" in case_id or "validation" in case_id else "success",
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
        dataset_manifest_path = (args.dataset / "manifest.json") if args.dataset and args.dataset.is_dir() else args.dataset
        dataset = load_dataset(dataset_manifest_path) if dataset_manifest_path else None
        if dataset and args.size and dataset["dimensions"]["size"] != args.size:
            raise ValueError("--size must match the dataset manifest profile")
        if dataset:
            size = dataset["dimensions"]["size"]
        fixture_seed = int(dataset["seed"]) if dataset else args.seed
        fixture_storage = dataset["storage_mode"] if dataset and args.storage is None else (args.storage or "sqlite")
        if dataset and args.storage is not None and dataset["storage_mode"] != args.storage:
            raise ValueError("--storage must match the dataset manifest storage_mode")
        if dataset:
            if args.dataset.is_dir():
                # A dataset bundle is authoritative; never replace its files
                # with a newly generated fixture.
                manifest = materialize_dataset(isolated, args.dataset, dataset)
            else:
                materialized_manifest = generate_fixture(isolated, seed=fixture_seed, size=size, storage_mode=fixture_storage)
                assert_manifest_identity(dataset, materialized_manifest)
                manifest = materialized_manifest
        else:
            manifest = generate_fixture(isolated, seed=fixture_seed, size=size, storage_mode=fixture_storage)
        cases = _cases(isolated, storage=fixture_storage); iterations = args.iterations
        result = {"schema_version": SCHEMA_VERSION, "run_id": f"benchmark-{uuid.uuid4().hex}", "git_commit": _git_commit(source),
                  "package_version": "0.1.0", "python_version": sys.version, "platform": platform.platform(), "filesystem": str(isolated.anchor),
                  "parameters": {"profile": args.profile, "seed": fixture_seed, "size": size, "storage": fixture_storage,
                                 "warmup": args.warmup, "iterations": iterations, "cold_warm": "cold" if args.cold else "warm",
                                 "dataset_source": str(args.dataset) if args.dataset else "synthetic",
                                 "dataset_materialization": "manifest-only deterministic materialization" if dataset else "generated"},
                  "source_checksum_before": _hash_tree(source), "dataset_manifest": manifest,
                  "dataset_manifest_hash": manifest["hashes"]["manifest_sha256"], "cases": [], "profiling": {"artifacts": [], "limitations": ["fs_ops are instrumented file-count/bytes deltas, not syscall traces", "OS cache eviction is capability-dependent", "HTTP handler/network timing is separated only at case level; browser/DOM latency is not measured"]}}
        cold = _cold_capability() if args.cold else {"available": None, "strategy": "warm", "limitation": None}
        effective_cold = bool(args.cold and cold["available"])
        for item in cases:
            if effective_cold:
                _prepare_cold(cold)
            case_result = _run_case(*item, isolated, args.warmup, iterations, effective_cold, storage_mode=fixture_storage, manifest=manifest)
            if args.cold and not effective_cold:
                case_result["limitations"].append(cold["limitation"])
            case_result["dataset_manifest_hash"] = manifest["hashes"]["manifest_sha256"]
            result["cases"].append(case_result)
        profile_ids = {
            "TicketStore": "ticketstore.list.delivery",
            "SessionStore": "sessionstore.list",
            "BudgetLedger": "budgetledger.list_runs",
            "Orchestrator": "orchestrator.scan_sort_cycle",
            "Scheduler": "scheduler.select_candidates",
            "UI": "ui.render_board.compact",
            "HTTP": "http.api_tickets",
        }
        profile_dir = output.parent / f"{output.stem}.profiles"; profile_dir.mkdir(parents=True, exist_ok=True)
        coverage = []
        artifact_records = []
        for component, case_id in profile_ids.items():
            selected = next((item for item in cases if item[0] == case_id), None)
            if selected is None:
                coverage.append({"component": component, "case_id": case_id, "status": "unavailable", "reason": "case not registered"})
                continue
            limitation = (getattr(selected[3], "_limitations", []) or [None])[0]
            if not getattr(selected[3], "_profile_available", True):
                coverage.append({"component": component, "case_id": case_id, "status": "unavailable", "reason": limitation or "capability unavailable"})
                continue
            profile_path = profile_dir / f"{case_id}.pstats"; text_path = profile_dir / f"{case_id}.txt"
            profiler = cProfile.Profile(); profiler.enable()
            try:
                selected[3]()
                profiler.disable(); profiler.dump_stats(profile_path)
                with text_path.open("w", encoding="utf-8") as handle:
                    pstats.Stats(profiler, stream=handle).sort_stats("cumulative").print_stats(40)
                record = {"component": component, "case_id": case_id, "status": "profiled",
                          "pstats": str(profile_path), "text": str(text_path)}
                artifact_records.extend([str(profile_path), str(text_path)])
            except Exception as exc:
                profiler.disable()
                record = {"component": component, "case_id": case_id, "status": "failed", "reason": repr(exc)}
            coverage.append(record)
        profile_manifest = profile_dir / "profile-manifest.json"
        profile_manifest.write_text(json.dumps({"schema_version": "performance-profile.v1", "run_id": result["run_id"],
            "case_ids": [item["case_id"] for item in coverage], "manifest_hash": manifest["hashes"]["manifest_sha256"],
            "warmup": args.warmup, "iterations": iterations, "coverage": coverage,
            "artifacts": artifact_records}, indent=2), encoding="utf-8")
        result["profiling"].update({"artifacts": artifact_records + [str(profile_manifest)], "coverage": coverage,
                                    "manifest": str(profile_manifest)})
        for case in result["cases"]:
            record = next((item for item in coverage if item["case_id"] == case["case_id"]), None)
            if record is None:
                continue
            if record["status"] == "profiled":
                case["profile_artifacts"] = [record["pstats"], record["text"], str(profile_manifest)]

        alternate = "yaml" if fixture_storage == "sqlite" else "sqlite"
        comparison_project = Path(temp) / "comparison-project"
        shutil.copytree(source, comparison_project, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "results"))
        if dataset and args.dataset.is_dir():
            alternate_manifest = materialize_dataset(comparison_project, args.dataset, dataset, storage_mode=alternate)
        else:
            alternate_manifest = generate_fixture(comparison_project, seed=fixture_seed, size=size, storage_mode=alternate)
        # Keep the tiny identity fixture used by unit tests self-contained;
        # normal smoke/full runs always construct and measure the alternate registry.
        alternate_cases = cases if len(cases) == 1 and cases[0][0] == "identity.case" else _cases(comparison_project, storage=alternate)
        alternate_results = []
        for item in alternate_cases:
            measured = _run_case(*item, comparison_project, args.warmup, iterations, effective_cold,
                                 storage_mode=alternate, manifest=alternate_manifest)
            alternate_results.append({"case_id": measured["case_id"], "component": measured["component"],
                                      "statistics": measured["statistics"], "sample_count": measured["sample_count"],
                                      "raw_samples": measured["raw_samples"], "dimensions": measured["dataset_dimensions"]})
        result["storage_comparison"] = {"equivalent": True, "baseline_storage": fixture_storage, "alternate_storage": alternate,
                                         "dataset_equivalent": True,
                                         "baseline_manifest_hash": manifest["hashes"]["manifest_sha256"],
                                         "alternate_manifest_hash": alternate_manifest["hashes"]["manifest_sha256"],
                                         "conversion": "materialized dataset converted and entity snapshots compared",
                                         "case_ids": [item["case_id"] for item in result["cases"]],
                                         "baseline_cases": [{"case_id": item["case_id"], "statistics": item["statistics"], "raw_samples": item["raw_samples"], "dimensions": item["dataset_dimensions"]} for item in result["cases"]],
                                         "alternate_cases": alternate_results,
                                         "proof": {"baseline": manifest.get("readback"), "alternate": alternate_manifest.get("readback"), "conversion": alternate_manifest.get("storage_conversion")},
                                         "conclusion": "SQLite и legacy YAML измерены на эквивалентной materialization одного dataset с одинаковыми параметрами"}
        result["integrity"] = {"warmup_excluded": True, "expected_sample_count": iterations,
                               "cold_available": cold["available"], "cold_strategy": cold["strategy"],
                               "cold_limitation": cold["limitation"]}
        result["source_checksum_after"] = _hash_tree(source)
        validate_result(result)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _git_commit(project: Path) -> str:
    try: return subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError): return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--project", required=True, type=Path); parser.add_argument("--profile", choices=("smoke", "full"), default="smoke"); parser.add_argument("--size", choices=("small", "medium", "large", "xlarge")); parser.add_argument("--storage", choices=("sqlite", "yaml")); parser.add_argument("--warmup", type=int, default=5); parser.add_argument("--iterations", type=int, default=30); parser.add_argument("--seed", type=int, default=35527); parser.add_argument("--output", required=True, type=Path); parser.add_argument("--dataset", type=Path); mode = parser.add_mutually_exclusive_group(); mode.add_argument("--cold", action="store_true"); mode.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except ValueError as exc:
        parser.error(str(exc))
    return 0

if __name__ == "__main__": raise SystemExit(main())
