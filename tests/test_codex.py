import asyncio
import json
from hashlib import sha256
from pathlib import Path

from vibe_orchestrator.codex import CodexRunner
from vibe_orchestrator.config import Stage, load_workflow
from vibe_orchestrator.tickets import TicketStore


def test_stage_execution_profile_overrides_runner_defaults(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    stage = load_workflow("delivery").by_id["development"]
    runner = CodexRunner(store, model="fallback-model", reasoning_effort="low")

    profile = runner.execution_profile(stage)

    assert profile == {"model": "gpt-5.4", "reasoning_effort": "medium"}


def test_runner_uses_medium_reasoning_effort_by_default_without_stage_override(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    runner = CodexRunner(store)
    stage = Stage(id="custom", title="Custom", kind="agent", reasoning_effort=None)

    profile = runner.execution_profile(stage)

    assert profile == {"model": "gpt-5.4", "reasoning_effort": "medium"}


def test_prompt_includes_run_id_and_execution_profile(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Document contract", description="Обновить traceability контракт.")
    stage = load_workflow("delivery").by_id["development"]
    runner = CodexRunner(store, model="gpt-5-test", reasoning_effort="medium")
    prompt_spec = runner.prompt_spec(stage)
    profile = runner.execution_profile(stage)

    contract = runner.prepare_execution_contract(stage, "run-123")

    prompt = runner._build_prompt(ticket, stage, contract)

    assert "Run ID: run-123" in prompt
    assert f"Prompt path: {prompt_spec.path}" in prompt
    assert f"Prompt version: {contract.prompt_version}" in prompt
    assert f'Model: {profile["model"]}' in prompt
    assert f'Reasoning effort: {profile["reasoning_effort"]}' in prompt
    assert "Title: Document contract" in prompt
    assert "Priority: 100" in prompt
    assert "Parent: none" in prompt
    assert "Обновить traceability контракт." in prompt


def test_execution_contract_versions_full_prompt_template(tmp_path: Path, monkeypatch):
    store = TicketStore(tmp_path)
    store.init()
    stage = load_workflow("delivery").by_id["development"]
    runner = CodexRunner(store, model="gpt-5-test", reasoning_effort="medium")

    baseline = runner.prepare_execution_contract(stage, "run-123")

    def wrapped_render_prompt(**kwargs):
        prompt = original_render_prompt(**kwargs)
        return f"{prompt}\nWrapper drift sentinel.\n"

    original_render_prompt = runner._render_prompt
    monkeypatch.setattr(runner, "_render_prompt", wrapped_render_prompt)

    drifted = runner.prepare_execution_contract(stage, "run-123")

    assert baseline.prompt_version != drifted.prompt_version
    assert baseline.prompt_contract != drifted.prompt_contract


def test_execution_contract_version_matches_persisted_prompt_contract(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    stage = load_workflow("delivery").by_id["development"]
    runner = CodexRunner(store, model="gpt-5-test", reasoning_effort="medium")

    contract = runner.prepare_execution_contract(stage, "run-123")

    assert contract.prompt_version == f"sha256:{sha256(contract.prompt_contract.encode('utf-8')).hexdigest()}"


def test_build_exec_args_exposes_model_and_reasoning_without_running_codex(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    runner = CodexRunner(store, codex_binary="codex-bin", model="gpt-5-test", reasoning_effort="medium")
    stage = load_workflow("delivery").by_id["development"]

    args = runner._build_exec_args(tmp_path / "result.json", contract=runner.prepare_execution_contract(stage, "run-123"))

    assert args[:2] == ["codex-bin", "exec"]
    assert "--model" in args
    assert "gpt-5.4" in args
    assert "-c" in args
    assert 'model_reasoning_effort="medium"' in args
    assert args[-1] == "-"


def test_run_reuses_active_run_and_persists_replay_metadata(tmp_path: Path, monkeypatch):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Replayable run", status="development")
    ticket.active_run = "run-123"
    store.save(ticket)
    stage = load_workflow("delivery").by_id["development"]
    runner = CodexRunner(store, codex_binary="codex-bin", model="gpt-5-test", reasoning_effort="medium")

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt_bytes: bytes):
            output_path = tmp_path / ".vibe" / "runs" / "run-123" / "result.json"
            output_path.write_text(
                json.dumps({"outcome": "completed", "summary": "ok", "details": "trace"}),
                encoding="utf-8",
            )
            return (b'{"event":"done"}\n', None)

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(runner.run(ticket, stage))

    manifest = json.loads((tmp_path / ".vibe" / "runs" / "run-123" / "run.json").read_text(encoding="utf-8"))
    prompt = (tmp_path / ".vibe" / "runs" / "run-123" / "prompt.txt").read_text(encoding="utf-8")
    prompt_contract = (tmp_path / ".vibe" / "runs" / "run-123" / "prompt.contract.txt").read_text(encoding="utf-8")

    assert result.outcome == "completed"
    assert captured["cwd"] == tmp_path
    assert manifest["run_id"] == "run-123"
    assert manifest["artifacts_path"] == ".vibe/runs/run-123"
    assert manifest["prompt_path"] == stage.prompt
    assert manifest["prompt_artifact_path"] == ".vibe/runs/run-123/prompt.txt"
    assert manifest["prompt_contract_artifact_path"] == ".vibe/runs/run-123/prompt.contract.txt"
    assert manifest["prompt_contract"] == prompt_contract
    assert manifest["prompt"] == prompt
    assert manifest["prompt_version"].startswith("sha256:")
    assert manifest["prompt_version"] == f"sha256:{sha256(prompt_contract.encode('utf-8')).hexdigest()}"
    assert manifest["prompt_version"] != runner.prompt_spec(stage).version
    assert manifest["model"] == "gpt-5.4"
    assert manifest["reasoning_effort"] == "medium"
    assert manifest["ticket_snapshot"] == {
        "ticket_title": "Replayable run",
        "ticket_priority": "100",
        "ticket_parent": "none",
        "ticket_description": "(пусто)",
    }
    assert manifest["version"]
    assert manifest["command"] == captured["args"]


def test_run_uses_prepared_execution_contract_without_reloading_prompt_metadata(tmp_path: Path, monkeypatch):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Immutable contract", status="development")
    stage = load_workflow("delivery").by_id["development"]
    runner = CodexRunner(store, codex_binary="codex-bin", model="gpt-5-test", reasoning_effort="medium")
    contract = runner.prepare_execution_contract(stage, "run-contract")

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt_bytes: bytes):
            output_path = tmp_path / ".vibe" / "runs" / "run-contract" / "result.json"
            output_path.write_text(
                json.dumps({"outcome": "completed", "summary": "ok", "details": ""}),
                encoding="utf-8",
            )
            return (b"", None)

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    def fail_prompt_spec(_stage):
        raise AssertionError("prompt metadata reloaded")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runner, "prompt_spec", fail_prompt_spec)

    result = asyncio.run(runner.run(ticket, stage, contract.run_id, contract=contract))

    manifest = json.loads((tmp_path / ".vibe" / "runs" / "run-contract" / "run.json").read_text(encoding="utf-8"))

    assert result.outcome == "completed"
    assert manifest["prompt_path"] == contract.prompt_path
    assert manifest["prompt_version"] == contract.prompt_version
    assert manifest["prompt_contract"] == contract.prompt_contract
