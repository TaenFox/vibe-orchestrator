"""Produce cProfile evidence using the benchmark's dataset/run contract."""
from __future__ import annotations

import argparse
import json
import pstats
import platform
import shutil
import sys
import tempfile
from pathlib import Path

_profile_dir = str(Path(__file__).resolve().parent)
if sys.path and sys.path[0] == _profile_dir:
    sys.path.pop(0)
_repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo_root / "src"))
sys.path.insert(1, str(_repo_root))
import cProfile

from benchmarks.performance.workloads import (assert_manifest_identity, generate_fixture,
                                              load_dataset, materialize_dataset)
from benchmarks.performance.run_benchmark import _cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="profile")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--manifest-hash")
    parser.add_argument("--storage", choices=("sqlite", "yaml"))
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--size", choices=("small", "medium", "large", "xlarge"))
    args = parser.parse_args()
    if args.warmup < 0 or args.iterations <= 0:
        parser.error("warmup must be non-negative and iterations must be positive")
    if not args.dataset or not args.manifest_hash:
        parser.error("--dataset and --manifest-hash are required for provenance-safe profiling")
    manifest_path = args.dataset / "manifest.json" if args.dataset.is_dir() else args.dataset
    manifest = load_dataset(manifest_path)
    manifest_hash = manifest["hashes"]["manifest_sha256"]
    if args.manifest_hash != manifest_hash:
        parser.error("manifest hash does not match dataset")
    if args.size and args.size != manifest["dimensions"]["size"]:
        parser.error("--size must match dataset manifest profile")
    storage = args.storage or manifest["storage_mode"]
    if args.storage and args.storage != manifest["storage_mode"]:
        parser.error("--storage must match dataset manifest storage_mode")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vibe-profile-") as temp:
        project = Path(temp) / "project"
        shutil.copytree(args.project, project, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "results"))
        if args.dataset.is_dir():
            materialized = materialize_dataset(project, args.dataset, manifest, storage_mode=storage)
        else:
            materialized = generate_fixture(project, seed=manifest["seed"], size=manifest["dimensions"]["size"], storage_mode=storage)
            assert_manifest_identity(manifest, materialized)
        match = next((case for case in _cases(project, storage=storage) if case[0] == args.scenario), None)
        if match is None:
            parser.error(f"unknown scenario: {args.scenario}")
        path = args.output / f"{args.scenario}.pstats"
        text_path = args.output / f"{args.scenario}.txt"
        profiler = cProfile.Profile(); profiler.enable()
        errors = []
        for _ in range(args.warmup):
            match[3]()
        for index in range(args.iterations):
            try:
                match[3]()
            except Exception as exc:
                errors.append({"sample_index": index, "error": repr(exc)})
        profiler.disable(); profiler.dump_stats(path)
        with text_path.open("w", encoding="utf-8") as handle:
            pstats.Stats(profiler, stream=handle).sort_stats("cumulative").print_stats(40)
    profile_manifest = args.output / "profile-manifest.json"
    components = {"TicketStore": "ticketstore.list.delivery", "SessionStore": "sessionstore.list",
                  "BudgetLedger": "budgetledger.list_runs", "Orchestrator": "orchestrator.scan_sort_cycle",
                  "Scheduler": "scheduler.select_candidates", "UI": "ui.render_board.compact", "HTTP": "http.api_tickets"}
    coverage = []
    for component, case_id in components.items():
        if case_id == args.scenario:
            coverage.append({"component": component, "case_id": case_id, "status": "failed" if errors else "profiled",
                             "pstats": str(path), "text": str(text_path), "errors": errors})
        else:
            coverage.append({"component": component, "case_id": case_id, "status": "unavailable",
                             "reason": "standalone invocation selected a different scenario"})
    profile_manifest.write_text(json.dumps({
        "schema_version": "performance-profile.v1", "run_id": args.run_id,
        "case_id": args.scenario, "manifest_hash": manifest_hash, "storage": storage,
        "warmup": args.warmup, "iterations": args.iterations, "tool": "cProfile",
        "tool_version": platform.python_version(), "coverage": coverage,
        "artifacts": [str(path), str(text_path)],
    }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
