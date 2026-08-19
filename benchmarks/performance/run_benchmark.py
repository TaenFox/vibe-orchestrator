"""One-command performance harness. Run from the repository root."""
from __future__ import annotations

import argparse
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
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
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
    from workloads import generate_fixture, load_dataset

SCHEMA_VERSION = "performance-result.v2"


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
        if case["sample_count"] != len(case["raw_samples"]):
            raise ValueError(f"sample count mismatch for {case.get('case_id')}")
        if any(sample["sample_index"] < 0 for sample in case["raw_samples"]):
            raise ValueError("invalid sample index")


def _fs_snapshot(root: Path) -> tuple[int, int]:
    files = [p for p in root.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def _cases(project: Path, *, storage: str = "sqlite") -> list[tuple[str, str, str, Callable[[], Any]]]:
    use_database = storage == "sqlite"
    store, sessions, workflow, ledger = TicketStore(project, use_database=use_database), None, load_workflow("delivery"), BudgetLedger(project)
    sessions = SessionStore(project, store, use_database=use_database)
    store.init(); sessions.init(); tickets = store.list("delivery")
    ticket_id = tickets[0].id
    budget_id = f"ticket:{ticket_id}"
    session_id = sessions.list()[0].id
    server, _thread = start_server(project, port=0, open_browser=False)
    base_url = f"http://127.0.0.1:{server.server_port}"
    def http(path: str) -> bytes:
        with urllib.request.urlopen(base_url + path, timeout=10) as response:
            return response.read()
    def http_error(path: str) -> bytes:
        try: return http(path)
        except urllib.error.HTTPError as exc: return exc.read()
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
        ("budgetledger.read_budget", "BudgetLedger", "read_budget", lambda: ledger.read_budget(budget_id)),
        ("budgetledger.get_budget", "BudgetLedger", "get_budget", lambda: ledger.get_budget(budget_id)),
        ("budgetledger.get_run", "BudgetLedger", "get_run", lambda: ledger.get_run("RUN-FIX-00000")),
        ("budgetledger.list_runs", "BudgetLedger", "list_runs", lambda: ledger.list_runs(budget_id)),
        ("budgetledger.reserve", "BudgetLedger", "reserve (isolated mutation)", lambda: ledger.reserve(f"RUN-BENCH-{uuid.uuid4().hex}", ticket_id, None, {"tokens": 1, "points": 1, "runs": 1})),
        ("budgetledger.get_missing", "BudgetLedger", "get_run(missing)", lambda: ledger.get_run("RUN-MISSING")),
        ("budgetledger.reconcile", "BudgetLedger", "reconcile", lambda: ledger.reconcile()),
        ("scheduler.select_candidates", "Scheduler", "select_candidates", lambda: select_candidates(workflow, tickets, set())),
        ("ui.render_board.compact", "UI", "render_board", lambda: render_board(store, {"delivery": workflow}, "delivery", sessions, mode="compact")),
        ("ui.render_fragment", "UI", "render_board_fragment", lambda: render_board_fragment(store, {"delivery": workflow}, "delivery", session_store=sessions)),
        ("http.fragment", "HTTP", "GET /fragment", lambda: http("/fragment?process=delivery")),
        ("http.api_tickets", "HTTP", "GET /api/tickets", lambda: http("/api/tickets")),
        ("http.api_sessions", "HTTP", "GET /api/sessions", lambda: http("/api/sessions")),
        ("http.api_session", "HTTP", "GET /api/sessions/{id}", lambda: http(f"/api/sessions/{session_id}")),
        ("http.error.missing_session", "HTTP", "GET missing session (4xx)", lambda: http_error("/api/sessions/SESSION-MISSING")),
    ]
    return cases


def _run_case(case_id: str, component: str, operation: str, fn: Callable[[], Any], project: Path, warmup: int, iterations: int, noisy: bool) -> dict[str, Any]:
    for _ in range(warmup):
        try: fn()
        except Exception: pass
    samples = []; errors = []; fs_root = project / ".vibe"
    for index in range(iterations):
        before = _fs_snapshot(fs_root); start_wall = time.perf_counter_ns(); start_cpu = time.process_time_ns(); error = None
        try: fn()
        except Exception as exc: error = type(exc).__name__; errors.append({"sample_index": index, "type": error})
        wall = (time.perf_counter_ns() - start_wall) / 1_000_000; cpu = (time.process_time_ns() - start_cpu) / 1_000_000; after = _fs_snapshot(fs_root)
        samples.append({"sample_index": index, "wall_ms": wall, "cpu_ms": cpu, "fs_ops": abs(after[0] - before[0]), "fs_bytes": abs(after[1] - before[1]), "sqlite_queries": None, "sqlite_lock_ms": None, "error": error})
    walls = [item["wall_ms"] for item in samples]
    return {"case_id": case_id, "component": component, "operation": operation, "storage_mode": "sqlite",
            "expected_outcome": "error" if ".error" in case_id or ".miss" in case_id or "validation" in case_id else "success",
            "workload": {"project": "isolated", "noisy_filesystem": noisy},
            "mode": "cold" if noisy else "warm", "sample_count": len(samples), "statistics": statistics_for(walls), "errors": errors, "raw_samples": samples}


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.project).resolve(); output = Path(args.output).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vibe-performance-") as temp:
        isolated = Path(temp) / "project"; shutil.copytree(source, isolated, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "results"))
        size = args.size or ("small" if args.profile == "smoke" else "medium")
        dataset = load_dataset(args.dataset) if args.dataset else None
        if dataset:
            size = dataset.get("dimensions", {}).get("size", size)
        manifest = generate_fixture(isolated, seed=args.seed, size=size, storage_mode=args.storage)
        cases = _cases(isolated, storage=args.storage); iterations = args.iterations
        result = {"schema_version": SCHEMA_VERSION, "run_id": f"benchmark-{uuid.uuid4().hex}", "git_commit": _git_commit(source),
                  "package_version": "0.1.0", "python_version": sys.version, "platform": platform.platform(), "filesystem": str(isolated.anchor),
                  "parameters": {"profile": args.profile, "seed": args.seed, "size": size, "storage": args.storage,
                                 "warmup": args.warmup, "iterations": iterations, "cold_warm": "cold" if args.cold else "warm",
                                 "dataset_source": str(args.dataset) if args.dataset else "synthetic"},
                  "source_checksum_before": _hash_tree(source), "dataset_manifest": manifest, "cases": [], "profiling": {"artifacts": [], "limitations": ["fs_ops are instrumented file-count/bytes deltas, not syscall traces", "OS cache eviction is capability-dependent", "HTTP handler/network timing is separated only at case level; browser/DOM latency is not measured"]}}
        for item in cases:
            case_result = _run_case(*item, isolated, args.warmup, iterations, args.cold)
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
        result["integrity"] = {"warmup_excluded": True, "cold_available": False if args.cold else None,
                               "cold_limitation": "OS filesystem cache eviction is unavailable in this harness" if args.cold else None}
        result["source_checksum_after"] = _hash_tree(source)
        validate_result(result)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _git_commit(project: Path) -> str:
    try: return subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError): return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--project", required=True, type=Path); parser.add_argument("--profile", choices=("smoke", "full"), default="smoke"); parser.add_argument("--size", choices=("small", "medium", "large", "xlarge")); parser.add_argument("--storage", choices=("sqlite", "yaml"), default="sqlite"); parser.add_argument("--warmup", type=int, default=5); parser.add_argument("--iterations", type=int, default=30); parser.add_argument("--seed", type=int, default=35527); parser.add_argument("--output", required=True, type=Path); parser.add_argument("--dataset", type=Path); parser.add_argument("--cold", action="store_true"); parser.add_argument("--warm", action="store_true")
    args = parser.parse_args(); run(args); return 0

if __name__ == "__main__": raise SystemExit(main())
