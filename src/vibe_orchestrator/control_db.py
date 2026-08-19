from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in ("payload_json", "manifest_json", "result_json", "usage_json"):
        if key in value and isinstance(value[key], str):
            value[key.removesuffix("_json")] = json.loads(value[key])
    return value


class ControlPlaneReader:
    """Read-only query facade for the migrated control-plane snapshot."""

    def __init__(self, project: str | Path, database: str | Path | None = None):
        root = Path(project).resolve()
        self.project = root
        self.database = Path(database).resolve() if database else root / ".vibe" / "control.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        if not self.database.exists():
            raise FileNotFoundError(self.database)
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [_decode(row) for row in db.execute(sql, params).fetchall()]

    def list_tickets(self, *, process: str | None = None, status: str | None = None,
                     limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        clauses, params = [], []
        if process is not None:
            clauses.append("process = ?"); params.append(process)
        if status is not None:
            clauses.append("status = ?"); params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._query(f"SELECT * FROM tickets {where} ORDER BY priority, updated_at DESC LIMIT ? OFFSET ?",
                           (*params, limit, offset))

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
        return rows[0] if rows else None

    def list_sessions(self, *, status: str | None = None, limit: int = 100,
                      offset: int = 0) -> list[dict[str, Any]]:
        if status is None:
            return self._query("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset))
        return self._query("SELECT * FROM sessions WHERE status = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                           (status, limit, offset))

    def list_runs(self, *, ticket_id: str | None = None, limit: int = 100,
                  offset: int = 0) -> list[dict[str, Any]]:
        if ticket_id is None:
            return self._query("SELECT * FROM runs ORDER BY source_path DESC LIMIT ? OFFSET ?", (limit, offset))
        return self._query("SELECT * FROM runs WHERE ticket_id = ? ORDER BY source_path DESC LIMIT ? OFFSET ?",
                           (ticket_id, limit, offset))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def list_results(self, *, run_id: str | None = None, limit: int = 100,
                     offset: int = 0) -> list[dict[str, Any]]:
        if run_id is None:
            return self._query("SELECT * FROM run_results ORDER BY source_path DESC LIMIT ? OFFSET ?", (limit, offset))
        return self._query("SELECT * FROM run_results WHERE run_id = ?", (run_id,))

    def list_events(self, run_id: str, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM run_events WHERE run_id = ? ORDER BY event_index LIMIT ? OFFSET ?",
                           (run_id, limit, offset))

    def get_token_usage(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            return self._query("SELECT * FROM token_usage ORDER BY run_id")
        return self._query("SELECT * FROM token_usage WHERE run_id = ?", (run_id,))

    def counts(self) -> dict[str, int]:
        tables = ("tickets", "sessions", "runs", "prompt_contracts", "run_results", "run_events", "token_usage", "events")
        with self._connect() as db:
            return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


@dataclass(frozen=True)
class IntegrityReport:
    counts: dict[str, int]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def audit_control_plane(project: str | Path, database: str | Path | None = None) -> IntegrityReport:
    """Check cross-references and compare the SQLite snapshot with local artifacts."""
    root = Path(project).resolve()
    reader = ControlPlaneReader(root, database)
    counts = reader.counts()
    errors: list[str] = []
    warnings: list[str] = []
    ticket_files = list((root / ".vibe" / "tickets").glob("*/*.yaml"))
    session_files = list((root / ".vibe" / "sessions").glob("*.yaml"))
    manifest_files = list((root / ".vibe" / "runs").glob("*/run.json"))
    expected = {"tickets": len(ticket_files), "sessions": len(session_files), "runs": len(manifest_files)}
    for name, value in expected.items():
        if counts[name] != value:
            errors.append(f"{name}: SQLite={counts[name]}, files={value}")

    ticket_ids = {row["ticket_id"] for row in reader._query("SELECT ticket_id FROM tickets")}
    prompt_rows = {row["prompt_hash"]: row["prompt_text"] for row in reader._query("SELECT prompt_hash, prompt_text FROM prompt_contracts")}
    run_rows = reader.list_runs(limit=1_000_000)
    for row in run_rows:
        if row["ticket_id"] and row["ticket_id"] not in ticket_ids:
            errors.append(f"run {row['run_id']} references missing ticket {row['ticket_id']}")
        if not (root / row["source_path"]).exists():
            errors.append(f"run {row['run_id']} source is missing: {row['source_path']}")
        if row.get("prompt_hash"):
            prompt_text = prompt_rows.get(row["prompt_hash"])
            if prompt_text is None or hashlib.sha256(prompt_text.encode("utf-8")).hexdigest() != row["prompt_hash"]:
                errors.append(f"run {row['run_id']} has an invalid prompt reference")
    for row in reader._query("SELECT session_id, ticket_id FROM session_members"):
        if row["ticket_id"] not in ticket_ids:
            errors.append(f"session {row['session_id']} references missing ticket {row['ticket_id']}")
    result_run_ids = {row["run_id"] for row in reader.list_results(limit=1_000_000)}
    run_ids = {row["run_id"] for row in run_rows}
    for run_id in sorted(result_run_ids - run_ids):
        errors.append(f"result references missing run {run_id}")
    if counts["runs"] > counts["run_results"]:
        warnings.append(f"{counts['runs'] - counts['run_results']} runs have no result.json")
    if counts["runs"] > counts["token_usage"]:
        warnings.append(f"{counts['runs'] - counts['token_usage']} runs have no token_usage")
    return IntegrityReport(counts, tuple(errors), tuple(warnings))
