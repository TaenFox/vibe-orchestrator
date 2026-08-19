"""One-command performance harness. Run from the repository root."""
from __future__ import annotations

import argparse
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
from typing import Any, Callable

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
try:
    from .workloads import generate_fixture, load_dataset
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from benchmarks.performance.workloads import generate_fixture, load_dataset

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


class InstrumentedLedger(BudgetLedger):
    def __init__(self, project: Path, metrics: SQLiteMetrics) -> None:
        self.metrics = metrics
        super().__init__(project)

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()

        def trace(statement: str) -> None:
            normalized = statement.strip().upper()
            self.metrics.queries += 1
            if normalized.startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT")):
                self.metrics.transactions += 1

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
        if case["sample_count"] != len(case["raw_samples"]):
            raise ValueError(f"sample count mismatch for {case.get('case_id')}")
        for sample in case["raw_samples"]:
            if sample.get("sample_index", -1) < 0 or "wall_ms" not in sample or "error" not in sample:
                raise ValueError("invalid sample schema")
        sample_indices = {sample["sample_index"] for sample in case["raw_samples"]}
        if any(error.get("sample_index") not in sample_indices for error in case["errors"]):
            raise ValueError("error references an absent sample")


def _fs_snapshot(root: Path) -> tuple[int, int]:
    files = [p for p in root.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


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


def _cases(project: Path, *, storage: str = "sqlite") -> list[tuple[str, str, str, Callable[[], Any]]]:
    use_database = storage == "sqlite"
    sqlite_metrics = SQLiteMetrics()
    store, sessions, workflow, ledger = TicketStore(project, use_database=use_database), None, load_workflow("delivery"), InstrumentedLedger(project, sqlite_metrics)
    sessions = SessionStore(project, store, use_database=use_database)
    store.init(); sessions.init(); tickets = store.list("delivery")
    ticket_id = tickets[0].id
    budget_id = f"ticket:{ticket_id}"
    session_id = sessions.list()[0].id
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
        ("ticketstore.load_path", "TicketStore", "load_path", lambda: store.load_path(store.ticket_path(tickets[0])) if not store.database_enabled else store.get(ticket_id)),
        ("ticketstore.children_of", "TicketStore", "children_of", lambda: store.children_of(ticket_id)),
        ("sessionstore.list", "SessionStore", "list", lambda: sessions.list()),
        ("sessionstore.get", "SessionStore", "get", lambda: sessions.get(session_id)),
        ("sessionstore.load_path", "SessionStore", "load_path", lambda: sessions.load_path(sessions.session_path(session_id)) if not sessions.database_enabled else sessions.get(session_id)),
        ("sessionstore.membership_validation.error", "SessionStore", "invalid membership", lambda: sessions.create(["FIX-MISSING"])),
        ("sessionstore.add_membership", "SessionStore", "add_ticket", lambda: sessions.add_ticket(sessions.get(session_id), ticket_id)),
        ("sessionstore.remove_membership", "SessionStore", "remove_ticket", lambda: sessions.remove_ticket(sessions.get(session_id), ticket_id)),
        ("sessionstore.validation.overlap.error", "SessionStore", "overlap validation", lambda: sessions.create([ticket_id])),
        ("budgetledger.read_budget", "BudgetLedger", "read_budget", lambda: ledger.read_budget(budget_id)),
        ("budgetledger.get_budget", "BudgetLedger", "get_budget", lambda: ledger.get_budget(budget_id)),
        ("budgetledger.get_run", "BudgetLedger", "get_run", lambda: ledger.get_run("RUN-FIX-00000")),
        ("budgetledger.list_runs", "BudgetLedger", "list_runs", lambda: ledger.list_runs(budget_id)),
        ("budgetledger.reserve.idempotent", "BudgetLedger", "reserve (prepared idempotent)", lambda: ledger.reserve(prepared_run, ticket_id, None, {"tokens": 1, "points": 1, "runs": 1})),
        ("budgetledger.start", "BudgetLedger", "start (prepared lifecycle)", lambda: ledger.start(prepared_run)),
        ("budgetledger.finalize", "BudgetLedger", "finalize (isolated lifecycle)", lambda: ledger.finalize(prepared_run, "completed", {"tokens": 1, "points": 1, "runs": 1})),
        ("budgetledger.get_missing", "BudgetLedger", "get_run(missing)", lambda: ledger.get_run("RUN-MISSING")),
        ("budgetledger.reconcile", "BudgetLedger", "reconcile", lambda: ledger.reconcile()),
        ("budgetledger.release", "BudgetLedger", "release (prepared lifecycle)", lambda: ledger.release(prepared_run)),
        ("scheduler.select_candidates", "Scheduler", "select_candidates", lambda: select_candidates(workflow, tickets, set())),
        ("ui.render_board.compact", "UI", "render_board", lambda: render_board(store, {"delivery": workflow}, "delivery", sessions, mode="compact")),
        ("ui.render_fragment", "UI", "render_board_fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=sessions)),
        ("http.handler.fragment", "HTTP", "handler /fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=sessions)),
        ("http.handler.api_tickets", "HTTP", "handler /api/tickets", lambda: [ticket.to_dict() for ticket in store.list()]),
        ("http.fragment", "HTTP", "GET /fragment network", lambda: http("/fragment?process=delivery")),
        ("http.api_tickets", "HTTP", "GET /api/tickets", lambda: http("/api/tickets")),
        ("http.api_sessions", "HTTP", "GET /api/sessions", lambda: http("/api/sessions")),
        ("http.api_session", "HTTP", "GET /api/sessions/{id}", lambda: http(f"/api/sessions/{session_id}")),
        ("http.error.missing_session", "HTTP", "GET missing session (4xx)", lambda: http_error("/api/sessions/SESSION-MISSING")),
    ]
    if server is not None:
        # The server is intentionally kept alive for the returned closures and
        # closed by run() after all cases complete.
        for index, item in enumerate(cases):
            if index == len(cases) - 1:
                pass
    if storage != "sqlite":
        cases = [item for item in cases if not item[0].startswith("http.") or item[0].startswith("http.handler.")]
    if http_limitation:
        cases = [item for item in cases if not item[0].startswith("http.") or item[0].startswith("http.handler.")]
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
            error = type(exc).__name__
            if metrics is not None:
                metrics.errors += 1
            errors.append({"sample_index": index, "type": error})
        wall = (time.perf_counter_ns() - start_wall) / 1_000_000; cpu = (time.process_time_ns() - start_cpu) / 1_000_000; after = _fs_snapshot(fs_root)
        samples.append({"sample_index": index, "wall_ms": wall, "cpu_ms": cpu, "fs_ops": abs(after[0] - before[0]), "fs_bytes": abs(after[1] - before[1]),
                        "sqlite_queries": metrics.queries if metrics is not None else None,
                        "sqlite_transactions": metrics.transactions if metrics is not None else None,
                        "sqlite_lock_ms": metrics.lock_ms if metrics is not None else None,
                        "sqlite_errors": metrics.errors if metrics is not None else None,
                        "sqlite_metrics_unavailable_reason": None if metrics is not None else "case does not use SQLite",
                        "error": error})
    walls = [item["wall_ms"] for item in samples]
    return {"case_id": case_id, "component": component, "operation": operation, "storage_mode": storage_mode,
            "expected_outcome": "error" if ".error" in case_id or ".miss" in case_id or "validation" in case_id else "success",
            "dataset_dimensions": manifest["dimensions"],
            "sqlite_explain_query_plan": getattr(fn, "_sqlite_plans", []),
            "limitations": getattr(fn, "_limitations", []),
            "workload": {"project": "isolated", "noisy_filesystem": noisy, "fixture_checksum": manifest["fixture_files_sha256"]},
            "mode": "cold" if noisy else "warm", "sample_count": len(samples), "statistics": statistics_for(walls), "errors": errors, "raw_samples": samples}


def run(args: argparse.Namespace) -> dict[str, Any]:
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
        for item in cases:
            case_result = _run_case(*item, isolated, args.warmup, iterations, args.cold, storage_mode=args.storage, manifest=manifest)
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
        cold = _cold_capability() if args.cold else {"available": None, "strategy": "warm", "limitation": None}
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
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--project", required=True, type=Path); parser.add_argument("--profile", choices=("smoke", "full"), default="smoke"); parser.add_argument("--size", choices=("small", "medium", "large", "xlarge")); parser.add_argument("--storage", choices=("sqlite", "yaml"), default="sqlite"); parser.add_argument("--warmup", type=int, default=5); parser.add_argument("--iterations", type=int, default=30); parser.add_argument("--seed", type=int, default=35527); parser.add_argument("--output", required=True, type=Path); parser.add_argument("--dataset", type=Path); mode = parser.add_mutually_exclusive_group(); mode.add_argument("--cold", action="store_true"); mode.add_argument("--warm", action="store_true")
    args = parser.parse_args(); run(args); return 0

if __name__ == "__main__": raise SystemExit(main())
