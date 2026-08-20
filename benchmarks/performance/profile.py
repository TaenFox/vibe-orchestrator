"""Produce cProfile evidence for a benchmark scenario."""
from __future__ import annotations
import argparse, json, pstats, sys, platform, hashlib
from pathlib import Path
# Avoid shadowing the stdlib ``profile`` module while cProfile imports it.
_profile_dir = str(Path(__file__).resolve().parent)
if sys.path and sys.path[0] == _profile_dir:
    sys.path.pop(0)
import cProfile
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.performance.run_benchmark import _cases

PROFILE_SCHEMA_VERSION = "performance-profile.v1"


def artifact_descriptor(path: Path, kind: str, artifact_root: Path) -> dict[str, object]:
    """Return the portable, content-addressed description used by results."""
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
    seen: set[str] = set()
    root = artifact_root.resolve()
    for artifact in artifacts:
        if set(artifact) != {"path", "sha256", "size_bytes", "kind"}:
            raise ValueError("profiling artifact descriptor has an invalid schema")
        kind = artifact["kind"]
        if kind in seen or kind not in required:
            raise ValueError(f"invalid or duplicate profiling artifact kind: {kind}")
        seen.add(kind)
        path = (root / str(artifact["path"])).resolve()
        descriptor = artifact_descriptor(path, str(kind), root)
        if descriptor["sha256"] != artifact["sha256"] or descriptor["size_bytes"] != artifact["size_bytes"]:
            raise ValueError(f"profiling artifact checksum/size mismatch: {artifact['path']}")
    if seen != required:
        raise ValueError(f"profiling artifacts incomplete: missing {sorted(required - seen)}")

def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--project", type=Path, required=True); p.add_argument("--scenario", required=True); p.add_argument("--output", type=Path, required=True); p.add_argument("--run-id", default="profile"); p.add_argument("--manifest-hash", required=True); p.add_argument("--storage", choices=("sqlite", "yaml"), default="sqlite"); args = p.parse_args()
    match = next((case for case in _cases(args.project, storage=args.storage) if case[0] == args.scenario), None)
    if match is None: raise SystemExit(f"unknown scenario: {args.scenario}")
    args.output.mkdir(parents=True, exist_ok=True); path = args.output / f"{args.scenario}.pstats"; profiler = cProfile.Profile(); profiler.enable(); match[3](); profiler.disable(); profiler.dump_stats(path)
    with (args.output / f"{args.scenario}.txt").open("w", encoding="utf-8") as handle: pstats.Stats(profiler, stream=handle).sort_stats("cumulative").print_stats(40)
    text_path = args.output / f"{args.scenario}.txt"
    manifest_path = args.output / "profile-manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": PROFILE_SCHEMA_VERSION, "run_id": args.run_id,
        "case_id": args.scenario, "dataset_manifest_hash": args.manifest_hash, "manifest_hash": args.manifest_hash, "tool": "cProfile",
        "tool_version": platform.python_version(), "command": " ".join(sys.argv),
        "artifacts": [artifact_descriptor(path, "pstats", args.output), artifact_descriptor(text_path, "text", args.output)]}, indent=2), encoding="utf-8")
    validate_artifacts([artifact_descriptor(path, "pstats", args.output), artifact_descriptor(text_path, "text", args.output),
                        artifact_descriptor(manifest_path, "profile_manifest", args.output)], args.output)
    return 0
if __name__ == "__main__": raise SystemExit(main())
