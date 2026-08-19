#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from vibe_orchestrator.control_db_migration import migrate_control_plane


def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт .vibe control plane в SQLite")
    parser.add_argument("project", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="только проверить объём миграции")
    args = parser.parse_args()
    report = migrate_control_plane(args.project, args.database, dry_run=args.dry_run)
    mode = "проверка" if report.dry_run else "миграция"
    print(f"{mode}: database={report.database}")
    print(f"tickets={report.tickets} sessions={report.sessions} runs={report.runs} prompts={report.prompts} "
          f"applied_prompts={report.applied_prompts} results={report.results} run_events={report.run_events} "
          f"telemetry={report.telemetry} events={report.events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
