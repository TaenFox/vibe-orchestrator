import asyncio
import json
from pathlib import Path

from vibe_orchestrator.codex import CodexRunner
from vibe_orchestrator.config import load_workflow
from vibe_orchestrator.control_db_migration import migrate_control_plane
from vibe_orchestrator.tickets import TicketStore


def test_db_primary_runner_persists_runtime_text_without_run_files(tmp_path: Path, monkeypatch):
    historical = TicketStore(tmp_path, use_database=False)
    historical.init()
    ticket = historical.create("delivery", "task", "DB run", status="development")
    migrate_control_plane(tmp_path)
    store = TicketStore(tmp_path)
    ticket = store.get(ticket.id)
    runner = CodexRunner(store, codex_binary="codex-bin")
    stage = load_workflow("delivery").by_id["development"]

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt_bytes: bytes):
            output_path = Path(captured["args"][captured["args"].index("-o") + 1])
            output_path.write_text(json.dumps({"outcome": "completed", "summary": "stored"}), encoding="utf-8")
            return b'{"type":"turn.completed","timestamp":"2026-08-19T10:00:00+00:00","run_id":"run-db","model":"gpt-5.6-luna","reasoning_effort":"medium","usage_ref":"runtime-1","usage_semantics":"incremental","usage":{"input_tokens":4,"output_tokens":2}}\n', None

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(runner.run(ticket, stage, "run-db"))

    assert result.summary == "stored"
    import sqlite3
    with sqlite3.connect(tmp_path / ".vibe" / "control.sqlite3") as db:
        assert db.execute("select state from runs where run_id='run-db'").fetchone()[0] == "completed"
        assert db.execute("select summary from run_results where run_id='run-db'").fetchone()[0] == "stored"
        assert db.execute("select total_tokens from token_usage where run_id='run-db'").fetchone()[0] == 6
        assert db.execute("select count(*) from run_events where run_id='run-db'").fetchone()[0] == 1
        assert db.execute("select count(*) from run_prompts where run_id='run-db'").fetchone()[0] == 1
    assert not (tmp_path / ".vibe" / "runs" / "run-db" / "result.json").exists()
    assert not (tmp_path / ".vibe" / "runs" / "run-db" / "events.jsonl").exists()
