from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .control_db_migration import ensure_control_schema


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RunStore:
    """Transactional persistence for runtime text and run telemetry."""

    def __init__(self, database: str | Path):
        self.database = Path(database).resolve()
        ensure_control_schema(self.database)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def start(self, manifest: dict[str, Any], *, prompt_contract: str, prompt_text: str) -> None:
        run_id = manifest["run_id"]
        encoded = _json(manifest)
        contract_hash = _hash_text(prompt_contract)
        applied_hash = _hash_text(prompt_text)
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO prompt_contracts(prompt_hash,prompt_version,prompt_text,source_path) VALUES (?,?,?,?)",
                (contract_hash, manifest.get("prompt_version"), prompt_contract, manifest.get("prompt_path") or "runtime"),
            )
            db.execute(
                """INSERT INTO runs(run_id,ticket_id,process,stage,attempt_kind,state,started_at,terminal_at,source_path,manifest_json,prompt_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET state='started',started_at=excluded.started_at,manifest_json=excluded.manifest_json,prompt_hash=excluded.prompt_hash""",
                (run_id, manifest.get("ticket_id"), manifest.get("process"), manifest.get("stage"), manifest.get("attempt_kind"),
                 "started", _now(), None, f".vibe/runs/{run_id}/run.json", encoded, contract_hash),
            )
            db.execute("INSERT OR REPLACE INTO run_prompts(run_id,prompt_text,prompt_path,prompt_hash) VALUES (?,?,?,?)",
                       (run_id, prompt_text, manifest.get("prompt_path"), applied_hash))
            db.commit()

    def finish(self, run_id: str, manifest: dict[str, Any], stdout: bytes, *, result: dict[str, Any] | None,
               token_usage: dict[str, Any] | None, state: str) -> None:
        events = []
        for index, line in enumerate((stdout or b"").decode("utf-8", errors="replace").splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            events.append((run_id, index, str(payload.get("type") or payload.get("event") or "unknown"),
                           payload.get("timestamp"), _json(payload), f".vibe/runs/{run_id}/events.jsonl"))
        with self._db() as db:
            db.execute("UPDATE runs SET state=?, terminal_at=?, manifest_json=? WHERE run_id=?",
                       (state, _now(), _json(manifest), run_id))
            db.execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))
            db.executemany("INSERT INTO run_events VALUES (?,?,?,?,?,?)", events)
            if result is not None:
                db.execute(
                    "INSERT OR REPLACE INTO run_results VALUES (?,?,?,?,?,?,?)",
                    (run_id, result.get("outcome"), result.get("summary"), result.get("details"), _json(result),
                     f".vibe/runs/{run_id}/result.json", _hash_text(_json(result))),
                )
            if token_usage is not None:
                int_value = lambda key: token_usage.get(key) if isinstance(token_usage.get(key), int) and not isinstance(token_usage.get(key), bool) else None
                db.execute(
                    "INSERT OR REPLACE INTO token_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run_id, int_value("input_tokens"), int_value("output_tokens"), int_value("total_tokens"),
                     token_usage.get("model"), token_usage.get("source"), token_usage.get("usage_ref"),
                     token_usage.get("captured_at"), token_usage.get("normalization_version"), _json(token_usage)),
                )
            db.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def abort(self, run_id: str, *, reason: str) -> bool:
        """Mark an abandoned started run terminal without inventing a result."""
        with self._db() as db:
            updated = db.execute(
                "UPDATE runs SET state='aborted', terminal_at=? WHERE run_id=? AND state='started'",
                (_now(), run_id),
            ).rowcount
            db.commit()
        return bool(updated)
