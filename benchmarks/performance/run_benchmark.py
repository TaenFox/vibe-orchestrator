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
    from .workloads import generate_fixture, load_dataset
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from benchmarks.performance.workloads import generate_fixture, load_dataset

SCHEMA_VERSION = "performance-result.v2"


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
        self._cleanup_callback = cleanup or (lambda: None)
        self._cleaned = False

    def cleanup(self) -> None:
        """Release owned resources at most once."""
        if self._cleaned:
            return
        self._cleaned = True
        self._cleanup_callback()


_VOLATILE_FIELDS = {"created_at", "updated_at", "started_at", "completed_at", "cancelled_at",
                    "reserved_at", "started_at", "terminal_at", "timestamp", "consumed_at"}
_SNAPSHOT_FILE_CACHE: dict[str, tuple[int, int, Any]] = {}


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
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".yaml", ".yml", ".json"} and path.name not in {"manifest.json"}:
                try:
                    stat = path.stat()
                    cache_key = str(path)
                    fingerprint = (stat.st_mtime_ns, stat.st_size)
                    cached = _SNAPSHOT_FILE_CACHE.get(cache_key)
                    if cached is not None and cached[:2] == fingerprint:
                        loaded = cached[2]
                    else:
                        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                        _SNAPSHOT_FILE_CACHE[cache_key] = (*fingerprint, loaded)
                    files[str(path.relative_to(root))] = _normalize_logical(loaded)
                except (OSError, yaml.YAMLError):
                    files[str(path.relative_to(root))] = path.read_bytes().hex()
    payload = _normalize_logical({"entities": entities, "files": files})
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return {"hash": hashlib.sha256(encoded).hexdigest(), "entities": payload["entities"],
            "paths": sorted(files)}


class SQLiteMetrics:
    """Per-case SQLite trace counters; production connections are untouched."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.queries = 0
        self.transactions = 0
        self.errors = 0
        self.lock_ms = 0.0
        self.busy_errors = 0
        self._transaction_started = None


class InstrumentedLedger(BudgetLedger):
    def __init__(self, project: Path, metrics: SQLiteMetrics) -> None:
        self.metrics = metrics
        # Benchmark-only policy context: the registry exercises the supported
        # decision operations, so it must not fail merely because the fixture
        # ledger has no production authorizer configured.
        super().__init__(project, authorizer=lambda **_: (True, "benchmark-policy/v1"))

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()

        def trace(statement: str) -> None:
            normalized = statement.strip().upper()
            self.metrics.queries += 1
            if normalized.startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT")):
                self.metrics.transactions += 1
            if normalized.startswith("BEGIN"):
                self.metrics._transaction_started = time.perf_counter_ns()
            elif normalized in {"COMMIT", "ROLLBACK"} and getattr(self.metrics, "_transaction_started", None):
                self.metrics.lock_ms += (time.perf_counter_ns() - self.metrics._transaction_started) / 1_000_000
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
    if result["source_checksum_before"] != result["source_checksum_after"]:
        raise ValueError("benchmark mutated source project")
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
            sample_index = sample.get("sample_index")
            has_error = sample.get("error") is not None
            expected = case["expected_outcome"]
            if expected == "success" and has_error:
                raise ValueError(f"unexpected error for {case.get('case_id')} at sample_index {sample_index}")
            if expected == "error" and not has_error:
                raise ValueError(f"expected error missing for {case.get('case_id')} at sample_index {sample_index}")
            if kind == "read_only" and sample.get("isolation_clean") is not True:
                raise ValueError(
                    f"unclean read-only sample {case.get('case_id')} "
                    f"at sample_index {sample.get('sample_index')}"
                )
        sample_indices = {sample["sample_index"] for sample in case["raw_samples"]}
        if any(error.get("sample_index") not in sample_indices for error in case["errors"]):
            raise ValueError("error references an absent sample")
        error_indices = {error.get("sample_index") for error in case["errors"]}
        if case["expected_outcome"] == "error":
            for sample in case["raw_samples"]:
                if sample["sample_index"] not in error_indices:
                    raise ValueError(
                        f"error missing for {case.get('case_id')} at sample_index {sample['sample_index']}"
                    )
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


def _cases(project: Path, *, storage: str = "sqlite") -> CaseRegistry:
    use_database = storage == "sqlite"
    sqlite_metrics = SQLiteMetrics()
    store, sessions, workflow, ledger = TicketStore(project, use_database=use_database), None, load_workflow("delivery"), InstrumentedLedger(project, sqlite_metrics)
    sessions = SessionStore(project, store, use_database=use_database)
    store.init(); sessions.init(); tickets = store.list("delivery")
    ticket_id = next((item.id for item in tickets
                      if (ledger.get_budget(f"ticket:{item.id}") or {}).get("status") == "active"), tickets[0].id)
    second_ticket_id = next(item.id for item in tickets if item.id != ticket_id)
    budget_id = f"ticket:{ticket_id}"
    session_id = sessions.list()[0].id
    overlap_ticket_id = next(
        (ticket for item in sessions.list() if item.status in {"draft", "active"} for ticket in item.ticket_ids),
        ticket_id,
    )
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
    http_limitation = None if storage == "sqlite" else "HTTP loopback cases are unavailable in YAML registry mode"
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

    def isolated_session_creation(action: Callable[[], Any]) -> Any:
        """Clean sessions that are persisted before a validation exception."""
        existing = {item.id for item in sessions.list()}
        try:
            return action()
        finally:
            created = [item for item in sessions.list() if item.id not in existing]
            if sessions.database_enabled:
                with sessions._db() as db:
                    for item in created:
                        db.execute("DELETE FROM session_members WHERE session_id=?", (item.id,))
                        db.execute("DELETE FROM events WHERE entity_kind='session' AND entity_id=?", (item.id,))
                        db.execute("DELETE FROM sessions WHERE session_id=?", (item.id,))
            else:
                for item in created:
                    sessions.session_path(item).unlink(missing_ok=True)
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
                db.execute("DELETE FROM reconciliation_facts WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM adjustments WHERE adjusts_run_id=?", (run_id,))
                db.execute("DELETE FROM budget_decisions WHERE target_id IN (?, ?)", (owner, run_id))
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
        ("sessionstore.validation.overlap.error", "SessionStore", "overlap validation", lambda: isolated_session_creation(lambda: sessions.create([overlap_ticket_id]))),
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
        ("ticketstore.record_run_event", "TicketStore", "record_run_event", lambda: isolated_ticket(lambda item: store.record_run_event(item, run_id="BENCH", stage_id=None, event="benchmark"))),
        ("ticketstore.is_done", "TicketStore", "is_done", lambda: store.is_done(store.get(ticket_id))),
        ("ticketstore.run_path", "TicketStore", "run_path", lambda: store.run_path("RUN-FIX-00000")),
        ("sessionstore.effective_ticket_ids", "SessionStore", "effective_ticket_ids", lambda: sessions.effective_ticket_ids(sessions.get(session_id))),
        ("sessionstore.participants", "SessionStore", "participants", lambda: sessions.get(session_id).participants),
        ("sessionstore.inherit_ticket", "SessionStore", "inherit_ticket", lambda: isolated_session(lambda item: (sessions.activate(item), sessions.inherit_ticket(sessions.get(item.id), ticket_id, source_ticket=second_ticket_id)))),
        ("sessionstore.override_ticket", "SessionStore", "override_ticket", lambda: isolated_session(lambda item: (sessions.activate(item), sessions.override_ticket(sessions.get(item.id), ticket_id, actor="benchmark", reason="coverage")))),
        ("sessionstore.agent_add_ticket", "SessionStore", "agent_add_ticket", lambda: isolated_session(lambda item: sessions.agent_add_ticket(item.id, ticket_id, actor="benchmark", origin="benchmark"))),
        ("sessionstore.agent_remove_ticket", "SessionStore", "agent_remove_ticket", lambda: isolated_session(lambda item: sessions.agent_remove_ticket(item.id, second_ticket_id, actor="benchmark", origin="benchmark"))),
        ("sessionstore.agent_update_membership", "SessionStore", "agent_update_membership", lambda: isolated_session(lambda item: sessions.agent_update_membership(item.id, [{"ticket_id": second_ticket_id, "priority": 10}], actor="benchmark", origin="benchmark"))),
        ("budgetledger.create_budget", "BudgetLedger", "create_budget", lambda: isolated_budget(lambda owner, budget_id: ledger.get_budget(budget_id))),
        ("budgetledger.set_status", "BudgetLedger", "set_status", lambda: isolated_budget(lambda owner, budget_id: ledger.set_status(budget_id, "stop_new_runs"))),
        ("budgetledger.reserve", "BudgetLedger", "reserve", lambda: isolated_run(lambda run_id: ledger.reserve(run_id, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1}))),
        ("budgetledger.increase_limit", "BudgetLedger", "increase_limit", lambda: isolated_budget(lambda owner, _budget_id: ledger.increase_limit(actor="benchmark", target_scope="ticket", target_id=owner, dimension="tokens", delta=1, reason="coverage", reference="BENCH", one_shot=True))),
        ("budgetledger.allow_overrun", "BudgetLedger", "allow_overrun", lambda: isolated_budget(lambda owner, _budget_id: ledger.allow_overrun(actor="benchmark", target_scope="ticket", target_id=owner, dimensions=["tokens"], reason="coverage", reference="BENCH", one_shot=True))),
        ("budgetledger.resolve_unknown", "BudgetLedger", "resolve_unknown", lambda: isolated_run(lambda run_id: (ledger.start(run_id), ledger.finalize(run_id, "unknown", {"points": None}), ledger.resolve_unknown(actor="benchmark", run_id=run_id, reason="coverage", reference="BENCH", estimate={"points": 1}, confidence=0.8, one_shot=True)))),
        ("budgetledger.adjustment", "BudgetLedger", "adjustment", lambda: isolated_run(lambda run_id: (ledger.start(run_id), ledger.finalize(run_id, "unknown", {"points": None}), ledger.adjustment(run_id, {"tokens": 0, "points": 0, "runs": 0}, reason="coverage", author="benchmark")))),
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
    # Classification is a registry declaration, not an inference from case_id.
    # reconcile mutates ledger state even though its name has no mutation verb.
    mutation_case_ids = {
        "ticketstore.save", "ticketstore.create", "ticketstore.record_run_event",
        "sessionstore.create", "sessionstore.activate", "sessionstore.complete", "sessionstore.cancel",
        "sessionstore.add_membership", "sessionstore.remove_membership", "sessionstore.inherit_ticket",
        "sessionstore.override_ticket", "sessionstore.agent_add_ticket", "sessionstore.agent_remove_ticket",
        "sessionstore.agent_update_membership", "budgetledger.reconcile", "budgetledger.reserve.idempotent",
        "budgetledger.start", "budgetledger.finalize", "budgetledger.release", "budgetledger.reserve",
        "budgetledger.create_budget", "budgetledger.set_status", "budgetledger.increase_limit",
        "budgetledger.allow_overrun", "budgetledger.resolve_unknown", "budgetledger.adjustment",
        "ui.POST_create", "ui.POST_move", "ui.POST_retry", "ui.POST_release_retry",
        "ui.POST_session_add_remove_activate_complete_cancel", "ui.POST_workers", "ui.PATCH_agent_ticket",
        "ui.PATCH_agent_session",
    }
    read_only_case_ids = {
        "ticketstore.list.delivery", "ticketstore.list.all", "ticketstore.get.hit", "ticketstore.get.miss",
        "ticketstore.load_path", "ticketstore.children_of", "ticketstore.is_done", "ticketstore.run_path",
        "sessionstore.list", "sessionstore.get", "sessionstore.load_path", "sessionstore.membership_validation.error",
        "sessionstore.validation.overlap.error", "sessionstore.effective_ticket_ids", "sessionstore.participants",
        "budgetledger.read_budget", "budgetledger.get_budget", "budgetledger.get_run", "budgetledger.list_runs",
        "budgetledger.get_missing", "budgetledger.list_decisions", "budgetledger.list_reconciliation_facts",
        "budgetledger.concurrency.atomic_reserve",
        "scheduler.select_candidates", "scheduler.wip_count", "ui.render_board.compact", "ui.render_fragment",
        "http.handler.fragment", "http.handler.api_tickets", "http.fragment", "http.api_tickets",
        "http.api_sessions", "http.api_session", "http.error.missing_session", "http.error.unknown_endpoint",
        "http.transport.error", "ui.render_board", "ui.render_board_fragment", "ui.GET_board",
        "ui.GET_fragment", "ui.GET_drawer", "ui.GET_api_tickets", "ui.GET_api_sessions", "ui.GET_api_session",
        "ui.expected_4xx",
    }
    case_kinds = {case_id: "mutation" for case_id in mutation_case_ids}
    case_kinds.update({case_id: "read_only" for case_id in read_only_case_ids})
    if len(case_kinds) != len(cases) or {case[0] for case in cases} != set(case_kinds):
        raise AssertionError("benchmark registry kind declaration is incomplete or duplicated")
    specs = []
    for case_id, component, operation, fn in cases:
        # BudgetLedger owns the instrumented connection. Ticket/session stores
        # deliberately retain their internal connection lifecycle and report a
        # typed unavailable reason instead of pretending the counters are zero.
        setattr(fn, "_sqlite_metrics", sqlite_metrics if component == "BudgetLedger" else None)
        setattr(fn, "_sqlite_plans", _explain_plans(ledger) if component == "BudgetLedger" else [])
        if http_limitation and component in {"HTTP", "UI"}:
            setattr(fn, "_limitations", list(getattr(fn, "_limitations", [])) + [http_limitation])
        expected = "error" if ".error" in case_id or ".miss" in case_id or "validation" in case_id or "expected_4xx" in case_id or "transport" in case_id else "success"
        network_case = (component == "HTTP" and not case_id.startswith("http.handler")) or component == "UI" and (
            case_id.startswith(("ui.GET_", "ui.POST_", "ui.PATCH_")) or case_id == "ui.expected_4xx"
        )
        if network_case and http_limitation:
            expected = "error"
        if case_id.startswith(("ui.POST_", "ui.PATCH_")):
            expected = "error"
        modes = ("sqlite",) if component in {"HTTP", "UI"} and not case_id.startswith("http.handler") else ("sqlite", "yaml")
        specs.append(CaseSpec(case_id, component, operation, fn, case_kinds[case_id], storage_modes=modes, expected_outcome=expected,
                              limitations=list(getattr(fn, "_limitations", []))))
    def cleanup() -> None:
        if server is not None:
            try:
                server.shutdown()
            finally:
                server.server_close()

    return CaseRegistry(iter(specs), cleanup)


def _run_case(spec: CaseSpec, project: Path, warmup: int, iterations: int, noisy: bool, *, storage_mode: str, manifest: dict[str, Any]) -> dict[str, Any]:
    case_id, component, operation, fn = spec.case_id, spec.component, spec.operation, spec.run
    fs_root = project / ".vibe"
    warmup_before = _logical_snapshot(project)

    def execute_iteration() -> tuple[Exception | None, list[Exception]]:
        error = None
        teardown_errors: list[Exception] = []
        try:
            if spec.setup:
                spec.setup()
            fn()
        except Exception as exc:
            error = exc
        finally:
            if spec.teardown:
                try:
                    spec.teardown()
                except Exception as exc:
                    teardown_errors.append(exc)
        return error, teardown_errors

    for _ in range(warmup):
        warmup_error, teardown_errors = execute_iteration()
        if (spec.expected_outcome == "success" and warmup_error is not None) or teardown_errors:
            details = type(warmup_error).__name__ if warmup_error else type(teardown_errors[0]).__name__
            raise ValueError(f"warmup execution failed for {case_id}: {details}")
        if _logical_snapshot(project)["hash"] != warmup_before["hash"]:
            raise ValueError(f"warmup isolation failed for {case_id}")

    samples = []; errors = []; cleanup_errors: list[dict[str, Any]] = []; before_snapshot = _logical_snapshot(project)
    metrics = getattr(fn, "_sqlite_metrics", None)
    for index in range(iterations):
        if metrics is not None:
            metrics.reset()
        before = _fs_snapshot(fs_root); sample_snapshot = _logical_snapshot(project); start_wall = time.perf_counter_ns(); start_cpu = time.process_time_ns(); error = None
        callback_error, teardown_errors = execute_iteration()
        if callback_error is not None:
            error = type(callback_error).__name__
            if metrics is not None:
                metrics.errors += 1
            errors.append({"sample_index": index, "type": error})
        cleanup_errors.extend(
            {"sample_index": index, "type": type(item).__name__} for item in teardown_errors
        )
        wall = (time.perf_counter_ns() - start_wall) / 1_000_000; cpu = (time.process_time_ns() - start_cpu) / 1_000_000; after = _fs_snapshot(fs_root); after_snapshot = _logical_snapshot(project)
        samples.append({"sample_index": index, "wall_ms": wall, "cpu_ms": cpu, "fs_ops": abs(after[0] - before[0]), "fs_bytes": abs(after[1] - before[1]),
                        "sqlite_queries": metrics.queries if metrics is not None else None,
                        "sqlite_transactions": metrics.transactions if metrics is not None else None,
                        "sqlite_lock_ms": metrics.lock_ms if metrics is not None else None,
                        "sqlite_errors": metrics.errors if metrics is not None else None,
                        "sqlite_metrics_unavailable_reason": None if metrics is not None else "case does not use SQLite",
                        "error": error, "isolation_clean": after_snapshot["hash"] == sample_snapshot["hash"]})
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
        cases = _cases(isolated, storage=args.storage)
        atexit.register(cases.cleanup)
        try:
            iterations = args.iterations
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
                case_result = _run_case(item, isolated, args.warmup, iterations, effective_cold, storage_mode=args.storage, manifest=manifest)
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
                    "case_id": selected[0], "manifest_hash": manifest["hashes"]["manifest_sha256"],
                    "artifacts": [str(profile_path), str(text_path)]}, indent=2), encoding="utf-8")
                result["profiling"]["artifacts"] = [str(profile_path), str(text_path), str(profile_manifest)]
                for case in result["cases"]:
                    if case["case_id"] == selected[0]:
                        case["profile_artifacts"] = result["profiling"]["artifacts"]
            result["integrity"] = {"warmup_excluded": True, "expected_sample_count": iterations,
                                   "cold_available": cold["available"], "cold_strategy": cold["strategy"],
                                   "cold_limitation": cold["limitation"]}
            result["source_checksum_after"] = _hash_tree(source)
            validate_result(result)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            atexit.unregister(cases.cleanup)
            primary_error = sys.exc_info()[1]
            try:
                cases.cleanup()
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"benchmark registry cleanup failed: {cleanup_error!r}")
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
