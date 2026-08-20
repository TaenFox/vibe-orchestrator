"""Produce cProfile evidence using the benchmark's dataset/run contract."""
from __future__ import annotations

import argparse
import hashlib
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

PROFILE_SCHEMA_VERSION = "performance-profile.v1"


def artifact_descriptor(path: Path, kind: str, artifact_root: Path) -> dict[str, object]:
    """Describe a profiling artifact relative to its controlled output root."""
    path = path.resolve()
    root = artifact_root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"profile artifact path escapes artifact root: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"profile artifact is not a regular file: {relative}")
    data = path.read_bytes()
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "kind": kind}


def validate_artifacts(artifacts: list[dict[str, object]], artifact_root: Path) -> None:
    required = {"pstats", "text", "profile_manifest"}
    seen: list[str] = []
    root = artifact_root.resolve()
    for artifact in artifacts:
        if set(artifact) != {"path", "sha256", "size_bytes", "kind"}:
            raise ValueError("profiling artifact descriptor has an invalid schema")
        kind = str(artifact["kind"])
        if kind not in required or (kind == "profile_manifest" and kind in seen):
            raise ValueError(f"invalid or duplicate profiling artifact kind: {kind}")
        seen.append(kind)
        descriptor = artifact_descriptor((root / str(artifact["path"])).resolve(), kind, root)
        if descriptor["sha256"] != artifact["sha256"] or descriptor["size_bytes"] != artifact["size_bytes"]:
            raise ValueError(f"profiling artifact checksum/size mismatch: {artifact['path']}")
    if seen == ["profile_manifest"]:
        return
    if not {"pstats", "text", "profile_manifest"}.issubset(seen):
        raise ValueError("profiling artifacts incomplete")


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
        "schema_version": PROFILE_SCHEMA_VERSION, "run_id": args.run_id,
        "case_id": args.scenario, "manifest_hash": manifest_hash,
    }, indent=2), encoding="utf-8")
    artifacts = [artifact_descriptor(path, "pstats", args.output),
                 artifact_descriptor(text_path, "text", args.output),
                 artifact_descriptor(profile_manifest, "profile_manifest", args.output)]
    profile_manifest.write_text(json.dumps({
        "schema_version": PROFILE_SCHEMA_VERSION, "run_id": args.run_id,
        "case_id": args.scenario, "manifest_hash": manifest_hash,
        "artifacts": artifacts,
    }, indent=2), encoding="utf-8")
    validate_artifacts(artifacts, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
