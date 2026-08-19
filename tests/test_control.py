import asyncio
from pathlib import Path

import pytest

from vibe_orchestrator.codex import AgentResult, ExecutionContract
from vibe_orchestrator.config import PromptSpec
from vibe_orchestrator.control import DeliverySessionControl, WorkerControl
from vibe_orchestrator.orchestrator import Orchestrator


class BlockingRunner:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def execution_profile(self, stage):
        return {"model": stage.model or "test-model", "reasoning_effort": stage.reasoning_effort or "medium"}

    def prompt_spec(self, stage):
        return PromptSpec(path=stage.prompt or "stub.md", body="", version="sha256:stub")

    def prepare_execution_contract(self, stage, run_id=None):
        prompt = self.prompt_spec(stage)
        profile = self.execution_profile(stage)
        return ExecutionContract(
            run_id=run_id or "run-test",
            prompt_path=prompt.path,
            prompt_body=prompt.body,
            prompt_contract="prompt contract",
            prompt_version=prompt.version,
            model=profile["model"],
            reasoning_effort=profile["reasoning_effort"],
        )

    async def run(self, ticket, stage, run_id=None, *, contract=None):
        self.started.set()
        await self.release.wait()
        return AgentResult(outcome="completed", summary="done")


def test_worker_control_persists_zero_and_rejects_negative_values(tmp_path: Path):
    control = WorkerControl(tmp_path)

    control.set_limit(0)

    assert WorkerControl(tmp_path).get_limit() == 0
    with pytest.raises(ValueError):
        control.set_limit(-1)


def test_orchestrator_preserves_saved_limit_unless_startup_override_is_explicit(tmp_path: Path):
    control = WorkerControl(tmp_path)
    control.set_limit(0)

    preserved = Orchestrator(tmp_path)
    overridden = Orchestrator(tmp_path, max_agents=2)

    assert preserved.max_agents == 0
    assert overridden.max_agents == 2
    assert control.get_limit() == 2


def test_lowering_worker_limit_drains_running_agents_without_cancelling_them(tmp_path: Path):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path, max_agents=1)
        runner = BlockingRunner()
        orchestrator.runner = runner
        first = orchestrator.store.create("delivery", "task", "First", status="ready_for_review")
        second = orchestrator.store.create("delivery", "task", "Second", status="ready_for_review")

        await orchestrator._schedule_once()
        await runner.started.wait()

        assert set(orchestrator.running) == {first.id}
        orchestrator.worker_control.set_limit(0)
        await orchestrator._schedule_once()

        assert set(orchestrator.running) == {first.id}
        assert not orchestrator.running[first.id].cancelled()
        assert orchestrator.store.get(second.id).status == "ready_for_review"

        runner.release.set()
        await orchestrator.running[first.id]
        orchestrator._reap_finished()
        await orchestrator._schedule_once()

        assert orchestrator.running == {}
        assert orchestrator.store.get(second.id).status == "ready_for_review"

        orchestrator.worker_control.set_limit(1)
        await orchestrator._schedule_once()

        assert len(orchestrator.running) == 1
        await next(iter(orchestrator.running.values()))

    asyncio.run(scenario())


def test_delivery_session_control_reads_active_participants(tmp_path: Path):
    from vibe_orchestrator.sessions import SessionStore
    from vibe_orchestrator.tickets import TicketStore
    tickets = TicketStore(tmp_path)
    first = tickets.create("delivery", "task", "First")
    second = tickets.create("delivery", "task", "Second")
    sessions = SessionStore(tmp_path, tickets)
    session = sessions.create([first.id, second.id])
    sessions.activate(session)

    assert DeliverySessionControl(tmp_path).get_participants() == {first.id, second.id}


def test_delivery_session_control_uses_legacy_mode_without_active_session(tmp_path: Path):
    assert DeliverySessionControl(tmp_path).get_participants() is None


def test_delivery_session_control_ignores_legacy_marker(tmp_path: Path):
    path = tmp_path / ".vibe" / "tmp"
    path.mkdir(parents=True)
    (path / "delivery-session.yaml").write_text("active: true\nparticipants: [DEL-A]\n", encoding="utf-8")

    assert DeliverySessionControl(tmp_path).get_participants() is None
