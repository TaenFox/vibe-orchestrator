"""Produce cProfile evidence using the benchmark's dataset/run contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import pstats
import platform
import re
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

from benchmarks.performance.workloads import generate_fixture, load_dataset, materialize_dataset
from benchmarks.performance.run_benchmark import DEFAULT_SEED, REQUIRED_PROFILE_CASES, _cases, parse_seed

PROFILE_SCHEMA_VERSION = "performance-profile.v1"


def prepare_output(output: Path) -> None:
    """Remove only artifacts owned by this profiler before a rerun.

    Reusing an output directory must not retain a pstats/text pair for a case
    that is unavailable in the current environment (for example, loopback
    binding may be denied by a sandbox). Keep unrelated files intact.
    """
    output.mkdir(parents=True, exist_ok=True)
    for path in output.iterdir():
        if path.is_file() and (path.suffix in {".pstats", ".txt"} or path.name == "profile-manifest.json"):
            path.unlink()


def artifact_descriptor(path: Path, kind: str, artifact_root: Path, *, run_id: str,
                        case_id: str, component: str, manifest_hash: str,
                        command_hash: str) -> dict[str, object]:
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
    return {"run_id": run_id, "case_id": case_id, "component": component,
            "manifest_hash": manifest_hash, "path": relative.as_posix(),
            "kind": kind, "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "command_hash": command_hash}


def validate_artifacts(artifacts: list[dict[str, object]], artifact_root: Path) -> None:
    required = {"pstats", "text", "profile_manifest"}
    seen: list[str] = []
    root = artifact_root.resolve()
    for artifact in artifacts:
        required_fields = {"run_id", "case_id", "component", "manifest_hash", "path",
                           "sha256", "size_bytes", "kind", "command_hash"}
        if set(artifact) != required_fields:
            raise ValueError("profiling artifact descriptor has an invalid schema")
        kind = str(artifact["kind"])
        if kind not in required or (kind == "profile_manifest" and kind in seen):
            raise ValueError(f"invalid or duplicate profiling artifact kind: {kind}")
        seen.append(kind)
        descriptor = artifact_descriptor(
            (root / str(artifact["path"])).resolve(), kind, root,
            run_id=str(artifact["run_id"]), case_id=str(artifact["case_id"]),
            component=str(artifact["component"]), manifest_hash=str(artifact["manifest_hash"]),
            command_hash=str(artifact["command_hash"]),
        )
        # The manifest contains its own descriptor, so its final digest cannot
        # be embedded without a recursive checksum.  Path/schema are still
        # checked; data artifacts retain strict checksum validation.
        if kind != "profile_manifest" and (descriptor["sha256"] != artifact["sha256"] or descriptor["size_bytes"] != artifact["size_bytes"]):
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
    parser.add_argument("--seed", type=parse_seed, default=DEFAULT_SEED)
    args = parser.parse_args()
    if args.warmup < 0 or args.iterations <= 0:
        parser.error("warmup must be non-negative and iterations must be positive")
    if args.dataset:
        manifest_path = args.dataset / "manifest.json" if args.dataset.is_dir() else args.dataset
        manifest = load_dataset(manifest_path)
        manifest_hash = manifest["hashes"]["manifest_sha256"]
        if not args.manifest_hash:
            parser.error("--manifest-hash is required with --dataset")
        if args.manifest_hash != manifest_hash:
            parser.error("manifest hash does not match dataset")
        if args.size and args.size != manifest["dimensions"]["size"]:
            parser.error("--size must match dataset manifest profile")
        storage = args.storage or manifest["storage_mode"]
        if args.storage and args.storage != manifest["storage_mode"]:
            parser.error("--storage must match dataset manifest storage_mode")
    else:
        storage = args.storage or "sqlite"
        manifest = None
        manifest_hash = None
    prepare_output(args.output)
    command_hash = hashlib.sha256(json.dumps([
        "profile", args.run_id, args.scenario, args.seed, args.size, storage,
        args.warmup, args.iterations, manifest_hash,
    ], sort_keys=True).encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="vibe-profile-") as temp:
        project = Path(temp) / "project"
        shutil.copytree(args.project, project, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "results"))
        if args.dataset:
            materialized = materialize_dataset(project, args.dataset, manifest, storage_mode=storage)
        else:
            materialized = generate_fixture(project, seed=args.seed, size=args.size or "small", storage_mode=storage)
            manifest = materialized
            manifest_hash = materialized["hashes"]["manifest_sha256"]
        registry_cases = _cases(project, storage=storage)
        registry = {case[0]: case for case in registry_cases}
        if args.scenario not in registry:
            parser.error(f"unknown scenario: {args.scenario}")
        coverage = []
        artifacts = []
        for component, case_id in REQUIRED_PROFILE_CASES.items():
            match = registry.get(case_id)
            if match is None:
                coverage.append({"component": component, "case_id": case_id, "status": "unavailable",
                                 "reason": "required case is unavailable in this runtime/storage mode"})
                continue
            limitations = list(match.limitations)
            if limitations:
                coverage.append({"component": component, "case_id": case_id, "status": "unavailable",
                                 "reason": "; ".join(limitations), "sample_parameters": {"warmup": args.warmup, "iterations": args.iterations}})
                continue
            safe_id = case_id.replace("/", "_")
            path = args.output / f"{safe_id}.pstats"
            text_path = args.output / f"{safe_id}.txt"
            profiler = cProfile.Profile(); profiler.enable(); errors = []
            for _ in range(args.warmup):
                try: match[3]()
                except Exception: pass
            for index in range(args.iterations):
                try: match[3]()
                except Exception as exc: errors.append({"sample_index": index, "error": repr(exc)})
            profiler.disable(); profiler.dump_stats(path)
            with text_path.open("w", encoding="utf-8") as handle:
                pstats.Stats(profiler, stream=handle).sort_stats("cumulative").print_stats(40)
            text = text_path.read_text(encoding="utf-8")
            # cProfile reports temporary checkout paths. Keep the report useful
            # while making committed evidence portable across worktrees.
            text = re.sub(r"/(?:Users|private|tmp|var|home|opt)/[^\s:]+",
                          "<project-path>", text)
            text_path.write_text(text, encoding="utf-8")
            artifacts.extend([
                artifact_descriptor(path, "pstats", args.output, run_id=args.run_id,
                                    case_id=case_id, component=component,
                                    manifest_hash=manifest_hash, command_hash=command_hash),
                artifact_descriptor(text_path, "text", args.output, run_id=args.run_id,
                                    case_id=case_id, component=component,
                                    manifest_hash=manifest_hash, command_hash=command_hash),
            ])
            coverage.append({"component": component, "case_id": case_id,
                             "status": "failed" if errors else "profiled",
                             "artifact_paths": [str(path.relative_to(args.output)), str(text_path.relative_to(args.output))],
                             "errors": errors, "sample_parameters": {"warmup": args.warmup, "iterations": args.iterations}})
        registry_cases.cleanup()
    profile_manifest = args.output / "profile-manifest.json"
    provenance = {"source_kind": manifest["source_kind"],
                  "synthetic_only": manifest["source_kind"] == "synthetic",
                  "seed": manifest["seed"], "manifest_hash": manifest_hash,
                  "dataset_tree_sha256": manifest["dataset_tree_sha256"],
                  "command": list(sys.argv)}
    profile_manifest.write_text(json.dumps({
        "schema_version": PROFILE_SCHEMA_VERSION, "run_id": args.run_id,
        "case_id": args.scenario, "requested_scenario": args.scenario,
        "manifest": manifest, "dataset_manifest": manifest, "manifest_hash": manifest_hash, "storage": storage,
        "provenance": provenance,
        "warmup": args.warmup, "iterations": args.iterations, "coverage": coverage,
        "artifacts": artifacts,
    }, indent=2), encoding="utf-8")
    artifacts.append(artifact_descriptor(profile_manifest, "profile_manifest", args.output,
                                         run_id=args.run_id, case_id=args.scenario,
                                         component="profiling", manifest_hash=manifest_hash,
                                         command_hash=command_hash))
    profile_manifest.write_text(json.dumps({
        "schema_version": PROFILE_SCHEMA_VERSION, "run_id": args.run_id,
        "case_id": args.scenario, "requested_scenario": args.scenario,
        "manifest": manifest, "dataset_manifest": manifest, "manifest_hash": manifest_hash, "storage": storage,
        "provenance": provenance,
        "warmup": args.warmup, "iterations": args.iterations, "coverage": coverage,
        "artifacts": artifacts,
    }, indent=2), encoding="utf-8")
    validate_artifacts(artifacts, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
