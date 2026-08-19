#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from vibe_orchestrator.control_db import audit_control_plane


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка SQLite control plane")
    parser.add_argument("project", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    report = audit_control_plane(args.project, args.database)
    print(json.dumps({"ok": report.ok, "counts": report.counts, "errors": report.errors, "warnings": report.warnings},
                     ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
