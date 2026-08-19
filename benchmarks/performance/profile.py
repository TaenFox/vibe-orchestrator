"""Produce cProfile evidence for a benchmark scenario."""
from __future__ import annotations
import argparse, cProfile, json, pstats, sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.performance.run_benchmark import _cases

def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--project", type=Path, required=True); p.add_argument("--scenario", required=True); p.add_argument("--output", type=Path, required=True); args = p.parse_args()
    match = next((case for case in _cases(args.project) if case[0] == args.scenario), None)
    if match is None: raise SystemExit(f"unknown scenario: {args.scenario}")
    args.output.mkdir(parents=True, exist_ok=True); path = args.output / f"{args.scenario}.pstats"; profiler = cProfile.Profile(); profiler.enable(); match[3](); profiler.disable(); profiler.dump_stats(path)
    with (args.output / f"{args.scenario}.txt").open("w", encoding="utf-8") as handle: pstats.Stats(profiler, stream=handle).sort_stats("cumulative").print_stats(40)
    (args.output / "profile-manifest.json").write_text(json.dumps({"case_id": args.scenario, "tool": "cProfile", "tool_version": platform.python_version(),
        "command": " ".join(sys.argv), "artifacts": [str(path), str(args.output / f"{args.scenario}.txt")]}, indent=2), encoding="utf-8")
    return 0
if __name__ == "__main__": raise SystemExit(main())
