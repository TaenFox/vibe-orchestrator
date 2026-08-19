from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sessions import SessionStore
from .tickets import TicketStore


SCHEMA_VERSION = 2


@dataclass(frozen=True)
class MigrationReport:
    database: Path
    tickets: int
    sessions: int
    runs: int
    events: int
    prompts: int
    applied_prompts: int
    results: int
    run_events: int
    telemetry: int
    dry_run: bool = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    process TEXT NOT NULL,
    ticket_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_process_status ON tickets(process, status);
CREATE INDEX IF NOT EXISTS idx_tickets_parent ON tickets(parent_id);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT,
    source_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE TABLE IF NOT EXISTS session_members (
    session_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    membership_kind TEXT NOT NULL DEFAULT 'direct',
    PRIMARY KEY(session_id, ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_session_members_ticket ON session_members(ticket_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    ticket_id TEXT,
    process TEXT,
    stage TEXT,
    attempt_kind TEXT,
    state TEXT,
    started_at TEXT,
    terminal_at TEXT,
    source_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    prompt_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_ticket ON runs(ticket_id);
CREATE TABLE IF NOT EXISTS prompt_contracts (
    prompt_hash TEXT PRIMARY KEY,
    prompt_version TEXT,
    prompt_text TEXT NOT NULL,
    source_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompt_contracts_version ON prompt_contracts(prompt_version);
CREATE TABLE IF NOT EXISTS run_prompts (
    run_id TEXT PRIMARY KEY,
    prompt_text TEXT NOT NULL,
    prompt_path TEXT,
    prompt_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_results (
    run_id TEXT PRIMARY KEY,
    outcome TEXT,
    summary TEXT,
    details TEXT,
    result_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    result_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_results_outcome ON run_results(outcome);
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    event_index INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT,
    payload_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY(run_id, event_index)
);
CREATE INDEX IF NOT EXISTS idx_run_events_type ON run_events(event_type);
CREATE TABLE IF NOT EXISTS token_usage (
    run_id TEXT PRIMARY KEY,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    model TEXT,
    source TEXT,
    usage_ref TEXT,
    captured_at TEXT,
    normalization_version TEXT,
    usage_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_index INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(entity_kind, entity_id, event_index)
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
"""


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_rows(entity_kind: str, entity_id: str, events: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    rows = []
    for index, event in enumerate(events):
        rows.append((entity_kind, entity_id, index, str(event.get("event", "unknown")), event.get("timestamp"), _json(event)))
    return rows


def ensure_control_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
        if "prompt_hash" not in run_columns:
            db.execute("ALTER TABLE runs ADD COLUMN prompt_hash TEXT")
        backfilled = db.execute("SELECT value FROM migration_meta WHERE key = 'run_prompts_backfilled'").fetchone()
        if backfilled is None:
            for run_id, manifest_json in db.execute("SELECT run_id, manifest_json FROM runs"):
                try:
                    manifest = json.loads(manifest_json)
                except (TypeError, json.JSONDecodeError):
                    continue
                prompt = manifest.get("prompt") if isinstance(manifest, dict) else None
                if isinstance(prompt, str):
                    db.execute(
                        "INSERT OR IGNORE INTO run_prompts(run_id,prompt_text,prompt_path,prompt_hash) VALUES (?,?,?,?)",
                        (run_id, prompt, manifest.get("prompt_path"), _text_hash(prompt)),
                    )
            db.execute("INSERT INTO migration_meta(key,value) VALUES ('run_prompts_backfilled','1')")
        db.execute("INSERT OR REPLACE INTO migration_meta(key,value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        db.commit()


def migrate_control_plane(project: Path, database: Path | None = None, *, dry_run: bool = False) -> MigrationReport:
    """Import the YAML/file control plane into an idempotent SQLite snapshot."""
    project = project.resolve()
    database = (database or project / ".vibe" / "control.sqlite3").resolve()
    tickets_store = TicketStore(project, use_database=False)
    tickets_store.init()
    tickets = tickets_store.list()
    session_store = SessionStore(project, tickets_store, use_database=False)
    sessions = session_store.list()
    manifests = sorted((tickets_store.runs_root).glob("*/run.json"))

    events: list[tuple[Any, ...]] = []
    ticket_rows = []
    for ticket in tickets:
        payload = ticket.to_dict()
        ticket_rows.append((ticket.id, ticket.process, ticket.type, ticket.status, ticket.priority, ticket.parent,
                            ticket.created_at, ticket.updated_at, str(tickets_store.ticket_path(ticket).relative_to(project)),
                            _json(payload), _hash(payload)))
        events.extend(_event_rows("ticket", ticket.id, [*ticket.audit_events, *ticket.run_history]))

    session_rows = []
    member_rows = []
    for session in sessions:
        payload = session.to_dict()
        path = session_store.session_path(session)
        session_rows.append((session.id, session.title, session.status, session.created_at, session.updated_at,
                             session.started_at, session.completed_at, session.cancelled_at,
                             str(path.relative_to(project)), _json(payload), _hash(payload)))
        member_rows.extend((session.id, ticket_id, position, "direct") for position, ticket_id in enumerate(session.ticket_ids))
        direct_ids = set(session.ticket_ids)
        for position, ticket_id in enumerate(sorted(session_store.effective_ticket_ids(session)), start=len(session.ticket_ids)):
            if ticket_id not in direct_ids:
                member_rows.append((session.id, ticket_id, position, "effective"))
        events.extend(_event_rows("session", session.id, session.audit_events))

    run_rows = []
    prompt_rows: dict[str, tuple[Any, ...]] = {}
    applied_prompt_rows = []
    result_rows = []
    run_event_rows = []
    telemetry_rows = []
    for path in manifests:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("run_id"), str):
            continue
        prompt_path = path.parent / "prompt.contract.txt"
        try:
            prompt_text = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            prompt_text = manifest.get("prompt_contract") if isinstance(manifest.get("prompt_contract"), str) else ""
        prompt_hash = _text_hash(prompt_text) if prompt_text else None
        if prompt_hash:
            prompt_rows.setdefault(prompt_hash, (
                prompt_hash,
                manifest.get("prompt_version"),
                prompt_text,
                str(prompt_path.relative_to(project)) if prompt_path.exists() else str(path.relative_to(project)),
            ))
        run_rows.append((manifest["run_id"], manifest.get("ticket_id"), manifest.get("process"), manifest.get("stage"),
                         manifest.get("attempt_kind"), None, None, None,
                         str(path.relative_to(project)), _json(manifest), prompt_hash))
        run_id = manifest["run_id"]
        applied_prompt = manifest.get("prompt")
        if isinstance(applied_prompt, str):
            applied_prompt_rows.append((run_id, applied_prompt, manifest.get("prompt_path"), _text_hash(applied_prompt)))
        result_path = path.parent / "result.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result = None
        if isinstance(result, dict):
            result_rows.append((run_id, result.get("outcome"), result.get("summary"), result.get("details"),
                                _json(result), str(result_path.relative_to(project)), _hash(result)))
            usage = result.get("token_usage")
            if isinstance(usage, dict):
                telemetry_rows.append((run_id, _int_or_none(usage.get("input_tokens")),
                                       _int_or_none(usage.get("output_tokens")), _int_or_none(usage.get("total_tokens")),
                                       usage.get("model"), usage.get("source"), usage.get("usage_ref"),
                                       usage.get("captured_at"), usage.get("normalization_version"), _json(usage)))
        events_path = path.parent / "events.jsonl"
        try:
            event_lines = events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            event_lines = []
        for event_index, line in enumerate(event_lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            run_event_rows.append((run_id, event_index, str(event.get("type") or event.get("event") or "unknown"),
                                   event.get("timestamp"), _json(event), str(events_path.relative_to(project))))

    report = MigrationReport(database, len(ticket_rows), len(session_rows), len(run_rows), len(events), len(prompt_rows),
                             len(applied_prompt_rows), len(result_rows), len(run_event_rows), len(telemetry_rows), dry_run)
    if dry_run:
        return report

    ensure_control_schema(database)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        db.execute("DELETE FROM session_members")
        db.execute("DELETE FROM events")
        db.execute("DELETE FROM run_events")
        db.execute("DELETE FROM run_results")
        db.execute("DELETE FROM run_prompts")
        db.execute("DELETE FROM token_usage")
        db.execute("DELETE FROM runs")
        db.execute("DELETE FROM prompt_contracts")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM tickets")
        db.executemany("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?)", ticket_rows)
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", session_rows)
        db.executemany("INSERT INTO session_members VALUES (?,?,?,?)", member_rows)
        db.executemany("INSERT INTO prompt_contracts VALUES (?,?,?,?)", prompt_rows.values())
        db.executemany("INSERT INTO run_prompts VALUES (?,?,?,?)", applied_prompt_rows)
        db.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)", run_rows)
        db.executemany("INSERT INTO run_results VALUES (?,?,?,?,?,?,?)", result_rows)
        db.executemany("INSERT INTO run_events VALUES (?,?,?,?,?,?)", run_event_rows)
        db.executemany("INSERT INTO token_usage VALUES (?,?,?,?,?,?,?,?,?,?)", telemetry_rows)
        db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", events)
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "migrated_at": _now(),
            "source_project": str(project),
            "ticket_count": str(len(ticket_rows)),
            "session_count": str(len(session_rows)),
            "run_count": str(len(run_rows)),
        }
        db.executemany("INSERT OR REPLACE INTO migration_meta(key,value) VALUES (?,?)", meta.items())
        db.commit()
    return report
