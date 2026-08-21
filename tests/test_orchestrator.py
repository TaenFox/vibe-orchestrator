import asyncio
import json
import threading
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vibe_orchestrator.codex import AgentResult, ExecutionContract
from vibe_orchestrator.config import PromptSpec, load_workflow
from vibe_orchestrator.orchestrator import Orchestrator, recover_stale_run, resume_rework
from vibe_orchestrator.orchestrator import decide_human_gate
from vibe_orchestrator.run_store import RunStore
from vibe_orchestrator.scheduler import Candidate, select_candidates
from vibe_orchestrator.technical_debt import TechnicalDebtError, technical_debt_basis
from vibe_orchestrator.tickets import TicketStore, TicketWriteService, next_status_for_ticket, reset_failed_retry


CONFIRMED_USAGE = {
    "run_id": "run-ta",
    "input_tokens": 12,
    "output_tokens": 3,
    "total_tokens": 15,
    "model": "test-model",
    "reasoning_effort": "medium",
    "source": "provider",
    "usage_ref": "evt-ta",
    "captured_at": "2026-08-17T10:11:12+00:00",
    "normalization_version": "tokens_per_1000.v1",
}


def run_events(ticket):
    return [entry for entry in ticket.run_history if entry["event"] != "created"]


def test_session_admission_boundary_serializes_membership_writer_without_sleep(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path, max_agents=1)
    entered = threading.Event()
    release = threading.Event()
    writer_entered = threading.Event()

    def holder():
        with orchestrator.session_store.admission_lock():
            entered.set()
            release.wait(timeout=2)

    def writer():
        entered.wait(timeout=2)
        with orchestrator.session_store.admission_lock():
            writer_entered.set()

    first = threading.Thread(target=holder)
    second = threading.Thread(target=writer)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    assert not writer_entered.is_set()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert writer_entered.is_set()


class SuccessfulRunner:
    def available(self):
        return True

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
        return AgentResult(outcome="completed", summary=f"{stage.id} done", details="")


class CapturingRunner(SuccessfulRunner):
    def __init__(self):
        self.contracts = []

    async def run(self, ticket, stage, run_id=None, *, contract=None):
        self.contracts.append(contract)
        return await super().run(ticket, stage, run_id, contract=contract)


class FailingRunner:
    def __init__(self):
        self.profile = {"model": "gpt-5.6-luna", "reasoning_effort": "high"}
        self.prompt = PromptSpec(path="delivery/review.md", body="", version="sha256:stub")

    def available(self):
        return True

    def execution_profile(self, stage):
        return dict(self.profile)

    def prompt_spec(self, stage):
        return self.prompt

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
        raise RuntimeError("agent crashed")


class ContractErrorRunner(SuccessfulRunner):
    async def run(self, ticket, stage, run_id=None, *, contract=None):
        return AgentResult(
            outcome="completed",
            summary="Технический анализ завершен",
            details="""```yaml
implementation_required: false
delivery_tickets: []
tech_debt_candidates:
  version: tech_debt_candidates.v1
  candidates: []
  unexpected: true
```""",
        )


class BrokenPromptRunner(FailingRunner):
    def prompt_spec(self, stage):
        raise FileNotFoundError("prompt file disappeared")


class BlockingRunner(SuccessfulRunner):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, ticket, stage, run_id=None, *, contract=None):
        self.started.set()
        await self.release.wait()
        return await super().run(ticket, stage, run_id, contract=contract)


class MutablePromptRunner(SuccessfulRunner):
    def __init__(self):
        self.prompt = PromptSpec(path="delivery/review.md", body="", version="sha256:initial")

    def prompt_spec(self, stage):
        return self.prompt

    async def run(self, ticket, stage, run_id=None, *, contract=None):
        self.prompt = PromptSpec(path="delivery/review-v2.md", body="", version="sha256:mutated")
        return await super().run(ticket, stage, run_id, contract=contract)


class MutableFailingRunner(FailingRunner):
    async def run(self, ticket, stage, run_id=None, *, contract=None):
        self.prompt = PromptSpec(path="delivery/review-v2.md", body="", version="sha256:mutated")
        raise RuntimeError("agent crashed")


async def _schedule_and_wait(orchestrator: Orchestrator, ticket_id: str) -> None:
    await orchestrator._schedule_once()
    await orchestrator.running[ticket_id]


def test_schedule_prefers_later_workflow_stage_before_priority(tmp_path: Path):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path, max_agents=1)
        orchestrator.runner = SuccessfulRunner()
        earlier = orchestrator.store.create(
            "delivery",
            "task",
            "Earlier stage",
            priority=1,
            status="selected_for_session",
        )
        later = orchestrator.store.create(
            "delivery",
            "task",
            "Later stage",
            priority=100,
            status="ready_for_review",
        )

        await orchestrator._schedule_once()

        assert orchestrator.store.get(later.id).active_run is not None
        assert orchestrator.store.get(earlier.id).active_run is None
        await orchestrator.running[later.id]

    asyncio.run(scenario())


def test_review_needs_rework_creates_blocking_child(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    parent = orchestrator.store.create("delivery", "task", "Fix API contract", status="review")
    parent.active_run = "run-review"
    orchestrator.store.save(parent)

    workflow = load_workflow("delivery")
    orchestrator._apply_result(
        workflow,
        parent.id,
        workflow.by_id["review"],
        AgentResult(outcome="needs_rework", summary="Найдена регрессия", details="Тест на контракт падает."),
    )

    parent = orchestrator.store.get(parent.id)
    children = orchestrator.store.children_of(parent.id, process="delivery")

    assert parent.status == "review"
    assert len(parent.blocked_by) == 1
    assert len(children) == 1
    assert children[0].id == parent.blocked_by[0]
    assert children[0].type == "rework"
    assert children[0].status == "selected_for_session"
    assert children[0].rework_stage == "review"
    assert children[0].wip_exempt is True
    assert [entry["event"] for entry in run_events(parent)] == ["completed"]
    assert all(entry["run_id"] == "run-review" for entry in run_events(parent))


def test_agent_can_pause_for_binary_human_decision_and_resume_same_stage(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    ticket = orchestrator.store.create("delivery", "task", "Decision ticket", status="review")
    ticket.active_run = "run-review"
    orchestrator.store.save(ticket)
    workflow = load_workflow("delivery")
    result = AgentResult(
        outcome="needs_human_decision",
        summary="Нужно решение владельца",
        details="""context:\n  human_gate:\n    question: Принять no-SLO решение?\n    proposal: Зафиксировать no-SLO как решение владельца.\n""",
    )

    orchestrator._apply_result(workflow, ticket.id, workflow.by_id["review"], result)
    waiting = orchestrator.store.get(ticket.id)
    assert waiting.status == "review"
    assert waiting.blocked_reason == "human_decision_required"
    assert waiting.context["human_gate"]["status"] == "pending"

    decided = decide_human_gate(orchestrator.store, ticket.id, "agree")
    assert decided.status == "ready_for_review"
    assert decided.blocked_reason is None
    assert decided.context["human_gate"]["decision"] == "agree"


def test_review_rework_inherits_parent_active_session(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    parent = orchestrator.store.create("delivery", "task", "Session parent", status="review")
    session = orchestrator.session_store.create([parent.id])
    orchestrator.session_store.activate(session)
    parent.active_run = "run-review"
    orchestrator.store.save(parent)

    workflow = load_workflow("delivery")
    orchestrator._apply_result(
        workflow,
        parent.id,
        workflow.by_id["review"],
        AgentResult(outcome="needs_rework", summary="Нужна правка", details="Добавить тест."),
    )

    child = orchestrator.store.children_of(parent.id, process="delivery")[0]
    loaded_session = orchestrator.session_store.get(session.id)
    assert child.id in loaded_session.ticket_ids
    assert any(
        event["event"] == "ticket_inherited"
        and event["ticket_id"] == child.id
        and event["source_ticket"] == parent.id
        for event in loaded_session.audit_events
    )


def test_reconcile_rework_sessions_repairs_missing_membership(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    parent = orchestrator.store.create("delivery", "task", "Session parent", status="review")
    session = orchestrator.session_store.create([parent.id])
    orchestrator.session_store.activate(session)
    rework = orchestrator.store.create(
        "delivery", "rework", "Existing rework", parent=parent.id,
        status="selected_for_session", rework_stage="review",
    )

    orchestrator._reconcile_rework_sessions()

    loaded_session = orchestrator.session_store.get(session.id)
    assert rework.id in orchestrator.session_store.effective_ticket_ids(loaded_session)


def test_rework_schedule_uses_parent_and_session_budgets(tmp_path: Path):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path, max_agents=1)
        runner = BlockingRunner()
        orchestrator.runner = runner
        parent = orchestrator.store.create("delivery", "task", "Parent", status="review")
        child = orchestrator.store.create(
            "delivery", "rework", "Child rework", parent=parent.id,
            status="selected_for_session", rework_stage="review",
        )
        child.context = {"budget": {"planned": {"tokens": 5, "points": 1, "runs": 1}}}
        orchestrator.store.save(child)
        session = orchestrator.session_store.create([child.id])
        orchestrator.session_store.activate(session)
        orchestrator.ledger.create_budget("ticket", parent.id, limits={"tokens": 10, "points": 10, "runs": 1})
        orchestrator.ledger.create_budget("ticket", child.id, limits={"tokens": 100, "points": 100, "runs": 100})
        orchestrator.ledger.create_budget("session", session.id, limits={"tokens": 10, "points": 10, "runs": 1})

        await orchestrator._schedule_once()
        await runner.started.wait()
        run_id = orchestrator.store.get(child.id).active_run
        run = orchestrator.ledger.get_run(run_id)
        assert run["ticket_id"] == child.id
        assert run["parent_ticket_id"] == parent.id
        assert run["ticket_budget_id"] == f"ticket:{parent.id}"
        assert orchestrator.ledger.get_budget(f"ticket:{parent.id}")["aggregates"]["reserved"]["runs"] == 1
        assert orchestrator.ledger.get_budget(f"ticket:{child.id}")["aggregates"]["reserved"]["runs"] == 0
        assert orchestrator.ledger.get_budget(f"session:{session.id}")["aggregates"]["reserved"]["runs"] == 1

        runner.release.set()
        await orchestrator.running[child.id]

    asyncio.run(scenario())


def test_override_member_rework_uses_session_and_parent_budgets(tmp_path: Path):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path, max_agents=1)
        runner = BlockingRunner()
        orchestrator.runner = runner
        parent = orchestrator.store.create("delivery", "task", "Parent", status="review")
        child = orchestrator.store.create(
            "delivery", "rework", "Override rework", parent=parent.id,
            status="selected_for_session", rework_stage="review",
        )
        child.context = {"budget": {"planned": {"tokens": 5, "points": 1, "runs": 1}}}
        orchestrator.store.save(child)
        member = orchestrator.store.create("delivery", "task", "Session member", status="selected_for_session")
        session = orchestrator.session_store.create(
            [member.id], membership_policy="legacy",
            budget_limits={"tokens": 10, "points": 10, "runs": 1},
        )
        # An override is the effective membership for an active session even when
        # the ticket is not present in the session's direct ticket_ids.
        orchestrator.session_store.activate(session)
        orchestrator.session_store.override_ticket(
            session, child.id, actor="reviewer", reason="Approved rework scope",
        )
        override_events = [
            event for event in session.audit_events
            if event.get("event") == "membership_override"
        ]
        assert override_events[-1]["ticket_id"] == child.id
        assert override_events[-1]["actor"] == "reviewer"
        assert override_events[-1]["reason"] == "Approved rework scope"
        orchestrator.ledger.create_budget("ticket", parent.id, limits={"tokens": 10, "points": 10, "runs": 1})
        orchestrator.ledger.create_budget("ticket", child.id, limits={"tokens": 100, "points": 100, "runs": 100})
        orchestrator.ledger.create_budget("session", session.id, limits={"tokens": 10, "points": 10, "runs": 1})

        await orchestrator._schedule_once()
        await runner.started.wait()

        scheduled = orchestrator.store.get(child.id)
        run_id = scheduled.run_history[-1]["run_id"]
        run = orchestrator.ledger.get_run(run_id)
        assert run["session_id"] == session.id
        assert run["parent_ticket_id"] == parent.id
        assert run["ticket_budget_id"] == f"ticket:{parent.id}"
        assert run["session_budget_id"] == f"session:{session.id}"
        assert orchestrator.ledger.get_budget(f"ticket:{parent.id}")["aggregates"]["reserved"]["runs"] == 1
        assert orchestrator.ledger.get_budget(f"ticket:{child.id}")["aggregates"]["reserved"]["runs"] == 0
        assert orchestrator.ledger.get_budget(f"session:{session.id}")["aggregates"]["reserved"]["runs"] == 1

        runner.release.set()
        await orchestrator.running[child.id]

    asyncio.run(scenario())


def test_runtime_membership_guard_blocks_external_legacy_rework_before_runner_or_ledger(tmp_path: Path, monkeypatch):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path, max_agents=1)
        runner = CapturingRunner()
        orchestrator.runner = runner
        parent = orchestrator.store.create("delivery", "task", "Parent", status="review")
        external = orchestrator.store.create(
            "delivery", "rework", "External rework", parent=parent.id,
            status="selected_for_session", rework_stage="review",
        )
        member = orchestrator.store.create("delivery", "task", "Member", status="selected_for_session")
        session = orchestrator.session_store.create(
            [member.id], membership_policy="legacy",
            budget_limits={"tokens": 10, "points": 10, "runs": 1},
        )
        orchestrator.session_store.activate(session)

        def stale_candidates(workflow, tickets, running_ids, **kwargs):
            if workflow.id == "delivery":
                return [Candidate(external, "selected_for_session", "system_analysis", 1)]
            return []

        monkeypatch.setattr("vibe_orchestrator.orchestrator.select_candidates", stale_candidates)
        await orchestrator._schedule_once()

        external = orchestrator.store.get(external.id)
        assert external.active_run is None
        assert external.blocked_reason == "session_membership_required"
        assert external.last_outcome == "blocked_budget"
        assert runner.contracts == []
        assert orchestrator.ledger.get_budget(f"session:{session.id}")["aggregates"]["reserved"]["runs"] == 0

    asyncio.run(scenario())


def test_budget_admission_denial_is_blocked_without_failed_run(tmp_path: Path):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path, max_agents=1)
        runner = CapturingRunner()
        orchestrator.runner = runner
        ticket = orchestrator.store.create("delivery", "task", "Budget gate", status="ready_for_review")
        ticket.context = {"budget": {"planned": {"tokens": 11, "points": 2, "runs": 1}}}
        orchestrator.store.save(ticket)
        orchestrator.ledger.create_budget("ticket", ticket.id, limits={"tokens": 10, "points": 10, "runs": 1})

        await orchestrator._schedule_once()

        blocked = orchestrator.store.get(ticket.id)
        assert blocked.active_run is None
        assert blocked.blocked_reason == "budget_exceeded_tokens"
        assert blocked.last_outcome == "blocked_budget"
        assert [event["event"] for event in run_events(blocked)] == ["blocked"]
        assert not runner.contracts
        assert orchestrator.ledger.get_run("missing") is None

    asyncio.run(scenario())


def test_reservation_metadata_is_carried_to_contract_and_history(tmp_path: Path):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path, max_agents=1)
        runner = CapturingRunner()
        orchestrator.runner = runner
        ticket = orchestrator.store.create("delivery", "task", "Budget trace", status="ready_for_review")
        ticket.context = {"budget": {"planned": {"tokens": 5, "points": 1, "runs": 1}}}
        orchestrator.store.save(ticket)
        orchestrator.ledger.create_budget("ticket", ticket.id, limits={"tokens": 10, "points": 10, "runs": 2})

        await _schedule_and_wait(orchestrator, ticket.id)
        metadata = runner.contracts[0].reservation_metadata
        completed = orchestrator.store.get(ticket.id)

        assert metadata["contract_version"] == "budget.v1"
        assert metadata["ticket_budget_id"] == f"ticket:{ticket.id}"
        assert metadata["reserved"] == {"tokens": 5, "points": 1, "runs": 1}
        assert run_events(completed)[0]["reservation"] == metadata
        assert run_events(completed)[1]["reservation"] == metadata

    asyncio.run(scenario())
def test_rework_needs_rework_allows_three_review_attempts_then_stops(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    ticket = orchestrator.store.create(
        "delivery",
        "rework",
        "Repeat rework",
        status="review",
        rework_stage="review",
    )
    workflow = load_workflow("delivery")
    review = workflow.by_id["review"]
    for attempt in range(1, 4):
        current = orchestrator.store.get(ticket.id)
        current.active_run = f"run-rework-review-{attempt}"
        orchestrator.store.save(current)
        orchestrator._apply_result(
            workflow,
            ticket.id,
            review,
            AgentResult(outcome="needs_rework", summary="Нужен integration-тест", details="AC-7.4"),
        )
        updated = orchestrator.store.get(ticket.id)
        if attempt < 3:
            assert updated.status == "selected_for_session"
            assert updated.blocked_reason is None
        else:
            assert updated.status == "selected_for_session"
            assert updated.blocked_reason == "rework_cycle_stopped"

    updated = orchestrator.store.get(ticket.id)
    assert updated.last_outcome == "needs_rework"
    assert orchestrator.store.children_of(ticket.id, process="delivery") == []
    assert select_candidates(workflow, [updated], set()) == []


def test_manual_resume_rework_returns_to_analysis_queue(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "rework", "Resume rework", status="selected_for_session", rework_stage="review")
    ticket.blocked_reason = "rework_cycle_stopped"
    store.save(ticket)

    resumed = resume_rework(store, ticket.id)

    assert resumed.status == "selected_for_session"
    assert resumed.blocked_reason is None
    assert resumed.last_outcome == "manual_rework_resumed"
    assert resumed.run_history[-1]["event"] == "manual_rework_resumed"


def test_recover_stale_run_returns_ticket_to_source_queue(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Stale run", status="ready_for_review")
    run_id = "stale-run"
    RunStore(store.database).start(
        {"run_id": run_id, "ticket_id": ticket.id, "process": "delivery", "stage": "review"},
        prompt_contract="contract",
        prompt_text="prompt",
    )
    ticket.status = "review"
    ticket.active_run = run_id
    store.record_run_event(ticket, run_id=run_id, stage_id="review", event="started", from_status="ready_for_review", to_status="review")
    store.save(ticket)

    recovered = recover_stale_run(
        store,
        ticket.id,
        now=datetime.now(timezone.utc) + timedelta(minutes=31),
    )

    assert recovered.status == "ready_for_review"
    assert recovered.active_run is None
    assert recovered.last_outcome == "run_interrupted"
    assert recovered.run_history[-1]["event"] == "stale_run_recovered"
    assert RunStore(store.database).get(run_id)["state"] == "aborted"


def test_recover_stale_run_rejects_fresh_run(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Fresh run", status="development")
    run_id = "fresh-run"
    RunStore(store.database).start(
        {"run_id": run_id, "ticket_id": ticket.id, "process": "delivery", "stage": "development"},
        prompt_contract="contract",
        prompt_text="prompt",
    )
    ticket.status = "development"
    ticket.active_run = run_id
    store.record_run_event(ticket, run_id=run_id, stage_id="development", event="started", from_status="ready_for_development", to_status="development")
    store.save(ticket)

    with pytest.raises(ValueError, match="ещё не считается зависшим"):
        recover_stale_run(store, ticket.id)

    recovered = recover_stale_run(store, ticket.id, force=True)
    assert recovered.active_run is None
    assert recovered.status == "ready_for_development"


@pytest.mark.parametrize(
    ("ticket_type", "prompt"),
    [
        ("audit", "process_management/audit.md"),
        ("planning", "process_management/planning.md"),
        ("estimation", "process_management/estimation.md"),
    ],
)
def test_process_management_uses_type_specific_prompt(tmp_path: Path, ticket_type: str, prompt: str):
    orchestrator = Orchestrator(tmp_path)
    ticket = orchestrator.store.create("process_management", ticket_type, "Process work", status="in_progress")
    stage = orchestrator._stage_for_ticket(orchestrator.workflows["process_management"], orchestrator.workflows["process_management"].by_id["in_progress"], ticket)

    assert stage.prompt == prompt


def test_completed_stage_updates_ticket_context_and_revision(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    ticket = orchestrator.store.create("delivery", "task", "Context update", status="system_analysis")
    ticket.active_run = "run-context"
    orchestrator.store.save(ticket)

    workflow = load_workflow("delivery")
    orchestrator._apply_result(
        workflow,
        ticket.id,
        workflow.by_id["system_analysis"],
        AgentResult(
            outcome="completed",
            summary="Спецификация готова",
            details="""```yaml
context:
  goal:
    expected_result: Стабильный handoff
  acceptance_criteria:
    - id: AC-1
      requirement: Контекст сохраняется
```""",
        ),
    )

    updated = orchestrator.store.get(ticket.id)
    event = run_events(updated)[0]
    assert updated.context_revision == 1
    assert updated.context["goal"]["expected_result"] == "Стабильный handoff"
    assert event["context_revision_before"] == 0
    assert event["context_revision_after"] == 1


def test_schedule_records_started_and_completed_run_history(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = SuccessfulRunner()
    ticket = orchestrator.store.create("delivery", "task", "Ship durable history", status="ready_for_review")
    ticket.last_outcome = "needs_rework"
    ticket.last_summary = "Previous review failed"
    ticket.consecutive_failures = 2
    ticket.retry_after = "2026-01-01T00:00:30+00:00"
    orchestrator.store.save(ticket)

    asyncio.run(_schedule_and_wait(orchestrator, ticket.id))
    orchestrator._reap_finished()

    ticket = orchestrator.store.get(ticket.id)

    assert ticket.active_run is None
    assert ticket.last_outcome == "completed"
    assert ticket.last_summary == "review done"
    assert ticket.consecutive_failures == 0
    assert ticket.retry_after is None
    assert ticket.status == "ready_for_acceptance"
    assert [entry["event"] for entry in run_events(ticket)] == ["started", "completed"]
    assert run_events(ticket)[0]["run_id"] == run_events(ticket)[1]["run_id"]
    assert run_events(ticket)[0]["event"] == "started"
    assert run_events(ticket)[0]["artifacts_path"].endswith(run_events(ticket)[0]["run_id"])
    assert run_events(ticket)[0]["prompt_path"] == "delivery/review.md"
    assert run_events(ticket)[0]["prompt_version"] == "sha256:stub"
    assert run_events(ticket)[0]["model"] == "gpt-5.6-luna"
    assert run_events(ticket)[0]["reasoning_effort"] == "low"
    assert run_events(ticket)[0]["ticket_title"] == "Ship durable history"
    assert run_events(ticket)[0]["ticket_priority"] == "100"
    assert run_events(ticket)[0]["ticket_parent"] == "none"
    assert run_events(ticket)[0]["ticket_description"] == "(пусто)"
    assert run_events(ticket)[1]["to_status"] == "ready_for_acceptance"
    assert run_events(ticket)[1]["ticket_title"] == "Ship durable history"
    assert run_events(ticket)[1]["ticket_priority"] == "100"


def test_execute_records_failed_run_history(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = FailingRunner()
    ticket = orchestrator.store.create("delivery", "task", "Surface agent failure", status="review")
    ticket.active_run = "run-failed"
    orchestrator.store.record_run_event(ticket, run_id="run-failed", stage_id="review", event="started", from_status="ready_for_review")
    orchestrator.store.save(ticket)

    asyncio.run(orchestrator._execute(load_workflow("delivery"), ticket.id, "review", "run-failed"))

    ticket = orchestrator.store.get(ticket.id)

    assert ticket.active_run is None
    assert ticket.last_outcome == "failed"
    assert ticket.last_summary == "agent crashed"
    assert ticket.consecutive_failures == 1
    assert ticket.retry_after is not None
    assert [entry["event"] for entry in run_events(ticket)] == ["started", "failed"]
    assert ticket.run_history[-1]["summary"] == "agent crashed"
    assert ticket.run_history[-1]["prompt_path"] == "delivery/review.md"
    assert ticket.run_history[-1]["prompt_version"] == "sha256:stub"
    assert ticket.run_history[-1]["artifacts_path"] == ".vibe/runs/run-failed"
    assert ticket.run_history[-1]["consecutive_failures"] == 1
    assert ticket.run_history[-1]["retry_after"] == ticket.retry_after


def test_technical_debt_contract_error_does_not_enter_correction_flow(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Reject malformed debt", status="technical_analysis")
    idea.active_run = "run-contract-error"
    idea.blocked_by = ["existing-child"]
    idea.context = {"unchanged": True}
    idea.context_revision = 3
    orchestrator.store.save(idea)
    before = orchestrator.store.get(idea.id).to_dict()

    with pytest.raises(TechnicalDebtError) as caught:
        orchestrator._apply_result(
            load_workflow("discovery"),
            idea.id,
            load_workflow("discovery").by_id["technical_analysis"],
            AgentResult(
                outcome="completed",
                summary="Технический анализ завершен",
                details="""```yaml
implementation_required: false
delivery_tickets: []
tech_debt_candidates:
  version: tech_debt_candidates.v1
  candidates: []
  unexpected: true
```""",
            ),
        )

    assert caught.value.envelope["contract_version"] == "orchestrator.errors.v1"
    assert caught.value.code == "TECH_DEBT_INVALID"
    assert orchestrator.store.get(idea.id).to_dict() == before
    assert orchestrator.store.children_of(idea.id) == []


@pytest.mark.parametrize(
    "manifest_metadata",
    [
        {"run_id": "run-other", "ticket_id": "__SOURCE_ID__", "stage": "technical_analysis"},
        {"run_id": "run-source", "stage": "technical_analysis"},
    ],
)
def test_technical_debt_source_mismatch_does_not_mutate_control_plane(tmp_path: Path, manifest_metadata: dict):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Reject foreign evidence", status="technical_analysis")
    idea.active_run = "run-contract-error"
    idea.context = {"unchanged": True}
    orchestrator.store.save(idea)
    run_dir = tmp_path / ".vibe" / "runs" / "run-source"
    run_dir.mkdir(parents=True)
    manifest = {
        key: idea.id if value == "__SOURCE_ID__" else value
        for key, value in manifest_metadata.items()
    }
    (run_dir / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    before = orchestrator.store.get(idea.id).to_dict()

    with pytest.raises(TechnicalDebtError) as caught:
        orchestrator._apply_result(
            load_workflow("discovery"),
            idea.id,
            load_workflow("discovery").by_id["technical_analysis"],
            AgentResult(
                outcome="completed",
                summary="Технический анализ завершен",
                details="""
implementation_required: false
delivery_tickets: []
tech_debt_candidates:
  version: tech_debt_candidates.v1
  candidates:
    - problem: "Foreign source"
      evidence:
        path: README.md
        identifier: "Traceability MVP"
        observation: "Наблюдение"
      impact: "Риск"
      suggested_scope: "Проверить источник"
      source_ticket: "%s"
      source_stage: technical_analysis
      source_run: run-source
      type: task
      urgency: medium
      priority: 10
""" % idea.id,
            ),
        )

    assert caught.value.code == "TECH_DEBT_SOURCE_MISMATCH"
    assert caught.value.path == "tech_debt_candidates.candidates[0].source_run"
    assert orchestrator.store.get(idea.id).to_dict() == before
    assert orchestrator.store.children_of(idea.id) == []


def test_technical_debt_observation_capability_error_does_not_mutate_control_plane(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Reject unverified evidence", status="technical_analysis")
    idea.active_run = "run-contract-error"
    orchestrator.store.save(idea)
    run_dir = tmp_path / ".vibe" / "runs" / "run-source"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": "run-source", "ticket_id": idea.id, "stage": "technical_analysis"}), encoding="utf-8")
    (tmp_path / "README.md").write_text("Traceability MVP\n", encoding="utf-8")
    before = orchestrator.store.get(idea.id).to_dict()

    with pytest.raises(TechnicalDebtError) as caught:
        orchestrator._apply_result(
            load_workflow("discovery"),
            idea.id,
            load_workflow("discovery").by_id["technical_analysis"],
            AgentResult(
                outcome="completed",
                summary="Технический анализ завершен",
                details="""
implementation_required: false
delivery_tickets: []
tech_debt_candidates:
  version: tech_debt_candidates.v1
  candidates:
    - problem: "Unverified source"
      evidence:
        path: README.md
        identifier: "Traceability MVP"
        observation: "Наблюдение"
      impact: "Риск"
      suggested_scope: "Проверить источник"
      source_ticket: "%s"
      source_stage: technical_analysis
      source_run: run-source
      type: task
      urgency: medium
      priority: 10
""" % idea.id,
            ),
        )

    assert caught.value.code == "TECH_DEBT_PREFLIGHT_UNAVAILABLE"
    assert caught.value.path == "tech_debt_candidates.candidates[0].evidence.observation"
    assert orchestrator.store.get(idea.id).to_dict() == before
    assert orchestrator.store.children_of(idea.id) == []


def test_execute_does_not_record_technical_debt_contract_error(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = ContractErrorRunner()
    idea = orchestrator.store.create("discovery", "idea", "Reject malformed debt", status="technical_analysis")
    idea.active_run = "run-contract-error"
    orchestrator.store.save(idea)
    before = orchestrator.store.get(idea.id).to_dict()

    asyncio.run(orchestrator._execute(load_workflow("discovery"), idea.id, "technical_analysis", "run-contract-error"))

    assert orchestrator.store.get(idea.id).to_dict() == before
    assert orchestrator.store.children_of(idea.id) == []


def test_agent_failure_stops_after_three_attempts_and_can_be_reset(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = FailingRunner()
    ticket = orchestrator.store.create("delivery", "task", "Bound retries", status="review")
    workflow = load_workflow("delivery")

    retry_delays = []
    for attempt in range(1, 4):
        ticket = orchestrator.store.get(ticket.id)
        ticket.active_run = f"run-failed-{attempt}"
        orchestrator.store.save(ticket)
        asyncio.run(orchestrator._execute(workflow, ticket.id, "review", ticket.active_run))
        ticket = orchestrator.store.get(ticket.id)
        if ticket.retry_after:
            failed_at = datetime.fromisoformat(ticket.run_history[-1]["timestamp"])
            retry_delays.append((datetime.fromisoformat(ticket.retry_after) - failed_at).total_seconds())

    ticket = orchestrator.store.get(ticket.id)

    assert ticket.last_outcome == "failed"
    assert ticket.consecutive_failures == 3
    assert ticket.retry_after is None
    assert retry_delays == pytest.approx([5, 30], abs=0.1)
    assert select_candidates(workflow, [ticket], set()) == []
    assert reset_failed_retry(ticket) is True
    assert ticket.consecutive_failures == 0
    assert select_candidates(workflow, [ticket], set())[0].target_status == "review"


def test_schedule_preserves_execution_contract_when_prompt_changes_during_run(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = MutablePromptRunner()
    ticket = orchestrator.store.create("delivery", "task", "Ship durable history", status="ready_for_review")

    asyncio.run(_schedule_and_wait(orchestrator, ticket.id))

    ticket = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in run_events(ticket)] == ["started", "completed"]
    assert [entry["prompt_version"] for entry in run_events(ticket)] == ["sha256:initial", "sha256:initial"]
    assert [entry["prompt_path"] for entry in run_events(ticket)] == ["delivery/review.md", "delivery/review.md"]


def test_run_history_preserves_ticket_snapshot_after_later_ticket_edits(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = SuccessfulRunner()
    ticket = orchestrator.store.create(
        "delivery",
        "task",
        "Original title",
        description="Original description",
        priority=42,
        parent="DEL-PARENT",
        status="ready_for_review",
    )

    asyncio.run(_schedule_and_wait(orchestrator, ticket.id))

    edited = orchestrator.store.get(ticket.id)
    edited.title = "Edited title"
    edited.description = "Edited description"
    edited.priority = 7
    edited.parent = "DEL-CHANGED"
    orchestrator.store.save(edited)

    reloaded = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in run_events(reloaded)] == ["started", "completed"]
    assert all(entry["ticket_title"] == "Original title" for entry in run_events(reloaded))
    assert all(entry["ticket_priority"] == "42" for entry in run_events(reloaded))
    assert all(entry["ticket_parent"] == "DEL-PARENT" for entry in run_events(reloaded))
    assert all(entry["ticket_description"] == "Original description" for entry in run_events(reloaded))


def test_schedule_preserves_last_terminal_outcome_until_run_finishes(tmp_path: Path):
    async def scenario() -> None:
        orchestrator = Orchestrator(tmp_path)
        runner = BlockingRunner()
        orchestrator.runner = runner
        ticket = orchestrator.store.create("delivery", "task", "Keep last outcome", status="ready_for_review")
        ticket.last_outcome = "needs_rework"
        ticket.last_summary = "Regression found"
        orchestrator.store.save(ticket)

        await orchestrator._schedule_once()
        await runner.started.wait()

        scheduled = orchestrator.store.get(ticket.id)
        assert scheduled.active_run is not None
        assert scheduled.last_outcome == "needs_rework"
        assert scheduled.last_summary == "Regression found"
        assert [entry["event"] for entry in run_events(scheduled)] == ["started"]

        runner.release.set()
        await orchestrator.running[ticket.id]

    asyncio.run(scenario())


def test_execute_records_failed_run_history_when_prompt_metadata_breaks(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = BrokenPromptRunner()
    ticket = orchestrator.store.create("delivery", "task", "Surface prompt failure", status="review")
    ticket.last_outcome = "completed"
    ticket.last_summary = "Previous run passed"
    ticket.active_run = "run-broken"
    orchestrator.store.record_run_event(
        ticket,
        run_id="run-broken",
        stage_id="review",
        event="started",
        from_status="ready_for_review",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    orchestrator.store.save(ticket)

    asyncio.run(orchestrator._execute(load_workflow("delivery"), ticket.id, "review", "run-broken"))

    ticket = orchestrator.store.get(ticket.id)

    assert ticket.active_run is None
    assert ticket.last_outcome == "failed"
    assert ticket.last_summary == "prompt file disappeared"
    assert [entry["event"] for entry in run_events(ticket)] == ["started", "failed"]
    assert ticket.run_history[-1]["summary"] == "prompt file disappeared"
    assert ticket.run_history[-1]["model"] == "gpt-5.6-luna"
    assert ticket.run_history[-1]["reasoning_effort"] == "high"
    assert "prompt_path" not in ticket.run_history[-1]
    assert "prompt_version" not in ticket.run_history[-1]


def test_schedule_retries_execution_contract_preparation_failure(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = BrokenPromptRunner()
    ticket = orchestrator.store.create("delivery", "task", "Broken prompt", status="ready_for_review")

    asyncio.run(orchestrator._schedule_once())

    ticket = orchestrator.store.get(ticket.id)

    assert ticket.status == "review"
    assert ticket.active_run is None
    assert ticket.last_outcome == "failed"
    assert ticket.consecutive_failures == 1
    assert ticket.retry_after is not None
    assert ticket.id not in orchestrator.running
    assert [entry["event"] for entry in run_events(ticket)] == ["started", "failed"]
    assert ticket.run_history[-1]["summary"] == "prompt file disappeared"


def test_completed_run_history_reuses_started_metadata_when_stage_definition_drifts(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    ticket = orchestrator.store.create("delivery", "task", "Freeze run metadata", status="review")
    ticket.active_run = "run-drift"
    orchestrator.store.record_run_event(
        ticket,
        run_id="run-drift",
        stage_id="review",
        event="started",
        from_status="ready_for_review",
        prompt_path="delivery/review.md",
        prompt_version="sha256:stable",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    orchestrator.store.save(ticket)

    workflow = load_workflow("delivery")
    stage = workflow.by_id["review"]
    orchestrator.runner = SuccessfulRunner()
    orchestrator.runner.execution_profile = lambda _stage: {"model": "drifted-model", "reasoning_effort": "low"}
    orchestrator.runner.prompt_spec = lambda _stage: PromptSpec(path="delivery/review-v2.md", body="", version="sha256:drifted")

    orchestrator._apply_result(
        workflow,
        ticket.id,
        stage,
        AgentResult(outcome="completed", summary="review done", details=""),
    )

    ticket = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in run_events(ticket)] == ["started", "completed"]
    assert ticket.run_history[-1]["model"] == "gpt-5.6-luna"
    assert ticket.run_history[-1]["reasoning_effort"] == "high"
    assert ticket.run_history[-1]["prompt_path"] == "delivery/review.md"
    assert ticket.run_history[-1]["prompt_version"] == "sha256:stable"


def test_failed_run_history_reuses_started_metadata_when_stage_definition_drifts(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    runner = FailingRunner()
    orchestrator.runner = runner
    ticket = orchestrator.store.create("delivery", "task", "Freeze failure metadata", status="review")
    ticket.active_run = "run-failed-drift"
    orchestrator.store.record_run_event(
        ticket,
        run_id="run-failed-drift",
        stage_id="review",
        event="started",
        from_status="ready_for_review",
        prompt_path="delivery/review.md",
        prompt_version="sha256:stable",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    orchestrator.store.save(ticket)
    runner.profile = {"model": "drifted-model", "reasoning_effort": "low"}
    runner.prompt = PromptSpec(path="delivery/review-v2.md", body="", version="sha256:drifted")

    asyncio.run(orchestrator._execute(load_workflow("delivery"), ticket.id, "review", "run-failed-drift"))

    ticket = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in run_events(ticket)] == ["started", "failed"]
    assert ticket.run_history[-1]["model"] == "gpt-5.6-luna"
    assert ticket.run_history[-1]["reasoning_effort"] == "high"
    assert ticket.run_history[-1]["prompt_path"] == "delivery/review.md"
    assert ticket.run_history[-1]["prompt_version"] == "sha256:stable"


def test_failed_run_history_preserves_execution_contract_when_prompt_changes_during_run(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = MutableFailingRunner()
    ticket = orchestrator.store.create("delivery", "task", "Freeze failure metadata", status="ready_for_review")

    asyncio.run(_schedule_and_wait(orchestrator, ticket.id))

    ticket = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in run_events(ticket)] == ["started", "failed"]
    assert [entry["prompt_version"] for entry in run_events(ticket)] == ["sha256:stub", "sha256:stub"]
    assert [entry["prompt_path"] for entry in run_events(ticket)] == ["delivery/review.md", "delivery/review.md"]


@pytest.mark.parametrize("parent_stage", ["review", "acceptance"])
def test_completed_rework_unblocks_parent(tmp_path: Path, parent_stage: str):
    orchestrator = Orchestrator(tmp_path)
    parent = orchestrator.store.create("delivery", "task", "Parent task", status=parent_stage)
    child = orchestrator.store.create("delivery", "rework", "Child rework", parent=parent.id, status=parent_stage, rework_stage=parent_stage)
    parent.blocked_by = [child.id]
    parent.last_outcome = "needs_rework"
    orchestrator.store.save(parent)
    child.active_run = f"run-{parent_stage}"
    orchestrator.store.save(child)

    workflow = load_workflow("delivery")
    orchestrator._apply_result(
        workflow,
        child.id,
        workflow.by_id[parent_stage],
        AgentResult(outcome="completed", summary="Исправление принято", details=""),
    )

    parent = orchestrator.store.get(parent.id)
    child = orchestrator.store.get(child.id)

    assert child.status == "done"
    assert orchestrator.store.is_done(child)
    assert parent.blocked_by == []
    candidates = select_candidates(workflow, orchestrator.store.list("delivery"), set())
    assert [(candidate.ticket.id, candidate.target_status) for candidate in candidates if candidate.ticket.id == parent.id] == [
        (parent.id, parent_stage)
    ]
    assert [entry["event"] for entry in run_events(child)] == ["completed"]
    assert run_events(child)[0]["run_id"] == f"run-{parent_stage}"


def test_technical_analysis_creates_delivery_children_and_waits_for_completion(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "New onboarding", status="technical_analysis")
    idea.active_run = "run-ta"
    orchestrator.store.save(idea)

    workflow = load_workflow("discovery")
    orchestrator._apply_result(
        workflow,
        idea.id,
        workflow.by_id["technical_analysis"],
        AgentResult(
            outcome="completed",
            summary="Готово к инвестиционному решению",
            details="""Рекомендуем продолжить.

```yaml
implementation_required: true
delivery_tickets:
  - type: story
    title: "Story onboarding shell"
    description: Собрать базовый сценарий онбординга.
    priority: 20
    mandatory: true
  - type: task
    title: "Task analytics events"
    description: Добавить события для воронки.
    mandatory: true
```
""",
        ),
    )

    idea = orchestrator.store.get(idea.id)
    children = orchestrator.store.children_of(idea.id, process="delivery")

    assert idea.status == "investment_decision"
    assert [child.type for child in children] == ["story", "task"]
    assert all(child.status == "todo" for child in children)
    assert all(child.mandatory is True for child in children)
    assert idea.implementation_required is True
    assert next_status_for_ticket(orchestrator.store, idea) == "implementation"
    assert [entry["event"] for entry in run_events(idea)] == ["completed"]
    assert run_events(idea)[0]["run_id"] == "run-ta"

    idea.status = "implementation"
    orchestrator.store.save(idea)
    orchestrator._reconcile_tickets()
    assert orchestrator.store.get(idea.id).status == "implementation"

    for child in children:
        child.status = "done"
        orchestrator.store.save(child)
    orchestrator._reconcile_tickets()

    assert orchestrator.store.get(idea.id).status == "ready_for_validation"


def test_technical_debt_exact_replay_preserves_existing_child_during_reconciliation(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Existing technical debt", status="technical_analysis")
    candidate = {
        "problem": "Stale adapter boundary",
        "impact": "Risk",
        "suggested_scope": "Extract the adapter",
        "evidence": {"path": "README.md", "identifier": "Traceability MVP", "observation": "Observed"},
        "source_ticket": idea.id,
        "source_run": "run-ta-1",
    }
    key, basis = technical_debt_basis(candidate, tmp_path)
    existing_result = TicketWriteService(tmp_path).create_technical_debt_ticket(
        problem=candidate["problem"],
        evidence=candidate["evidence"],
        impact=candidate["impact"],
        suggested_scope=candidate["suggested_scope"],
        source_ticket=candidate["source_ticket"],
        source_stage="technical_analysis",
        source_run=candidate["source_run"],
        dedup_key=key,
        dedup_basis=basis,
        priority=7,
        origin="technical_analysis:run-ta-1",
        actor="test",
    )
    assert existing_result.ticket is not None
    existing = existing_result.ticket
    assert existing.status == "todo"
    assert existing.mandatory is False
    assert existing.parent is None
    assert existing.blocked_by == []
    assert existing.technical_debt_deferred is True
    assert existing.context["problem"] == candidate["problem"]
    assert existing.context["evidence"] == candidate["evidence"]
    assert existing.context["impact"] == candidate["impact"]
    assert existing.context["suggested_scope"] == candidate["suggested_scope"]
    assert existing.context["origin"] == {
        "source_ticket": idea.id,
        "source_stage": "technical_analysis",
        "source_run": "run-ta-1",
        "dedup_key": key,
    }
    assert orchestrator.store.get(existing.id).technical_debt_deferred is True
    assert existing.run_history[-1]["event"] == "created"
    existing.status = "selected_for_session"
    orchestrator.store.save(existing)
    before = orchestrator.store.get(existing.id).to_dict()
    before_audit = list(existing.audit_events)

    details = textwrap.dedent("""
implementation_required: true
delivery_tickets:
  - type: task
    title: Stale adapter boundary
    description: Rewritten by ordinary synchronization
    priority: 99
    mandatory: true
tech_debt_candidates:
  version: tech_debt_candidates.v1
  candidates:
    - problem: Stale adapter boundary
      suggested_scope: Extract the adapter
      evidence:
        path: README.md
        identifier: Traceability MVP
        observation: Observed
      impact: Risk
      source_ticket: %s
      source_stage: technical_analysis
      source_run: run-ta-1
      type: task
      urgency: medium
      priority: 7
    """ % idea.id)

    orchestrator._create_delivery_children(idea, details)
    orchestrator._create_delivery_children(idea, details)

    active_matches = [
        child for child in orchestrator.store.list("delivery")
        if child.dedup_key == key and not orchestrator.store.is_done(child)
    ]
    replayed = orchestrator.store.get(existing.id)
    assert [child.id for child in active_matches] == [existing.id]
    assert replayed.to_dict() == before
    assert replayed.audit_events == before_audit


def test_technical_analysis_resets_unselected_existing_child_to_todo(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Restore delivery queue", status="technical_analysis")
    child = orchestrator.store.create(
        "delivery", "story", "Existing delivery", parent=idea.id, status="selected_for_session"
    )
    idea.active_run = "run-ta"
    orchestrator.store.save(idea)

    workflow = load_workflow("discovery")
    orchestrator._apply_result(
        workflow,
        idea.id,
        workflow.by_id["technical_analysis"],
        AgentResult(
            outcome="completed",
            summary="Повторная синхронизация",
            details="""```yaml
implementation_required: true
delivery_tickets:
  - type: story
    title: "Existing delivery"
    description: "Актуальное описание"
    mandatory: true
```""",
        ),
    )

    assert orchestrator.store.get(child.id).status == "todo"


def test_technical_analysis_without_implementation_skips_implementation(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Configuration-only decision", status="technical_analysis")
    idea.active_run = "run-ta"
    orchestrator.store.save(idea)

    workflow = load_workflow("discovery")
    orchestrator._apply_result(
        workflow,
        idea.id,
        workflow.by_id["technical_analysis"],
        AgentResult(
            outcome="completed",
            summary="Реализация не требуется",
            details="""```yaml
implementation_required: false
delivery_tickets: []
```""",
        ),
    )

    idea = orchestrator.store.get(idea.id)

    assert idea.status == "investment_decision"
    assert idea.implementation_required is False
    assert orchestrator.store.children_of(idea.id, process="delivery") == []
    assert next_status_for_ticket(orchestrator.store, idea) == "ready_for_validation"


def test_invalid_technical_analysis_plan_requests_correction(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Ambiguous implementation", status="technical_analysis")
    idea.active_run = "run-ta"
    orchestrator.store.save(idea)

    workflow = load_workflow("discovery")
    orchestrator._apply_result(
        workflow,
        idea.id,
        workflow.by_id["technical_analysis"],
        AgentResult(outcome="completed", summary="Готово", details="""```yaml
delivery_tickets: []
```"""),
    )

    idea = orchestrator.store.get(idea.id)
    corrections = orchestrator.store.children_of(idea.id, process="discovery")

    assert idea.status == "technical_analysis"
    assert idea.implementation_required is None
    assert idea.last_outcome == "needs_correction"
    assert idea.blocked_by == [corrections[0].id]
    assert "implementation_required" in corrections[0].description


def test_invalid_technical_analysis_plan_preserves_token_usage_in_run_history(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Ambiguous implementation", status="technical_analysis")
    idea.active_run = "run-ta"
    orchestrator.store.save(idea)

    workflow = load_workflow("discovery")
    orchestrator._apply_result(
        workflow,
        idea.id,
        workflow.by_id["technical_analysis"],
        AgentResult(
            outcome="completed",
            summary="Готово",
            details="""```yaml
delivery_tickets: []
```""",
            token_usage=CONFIRMED_USAGE,
        ),
    )

    updated = orchestrator.store.get(idea.id)

    assert updated.last_outcome == "needs_correction"
    assert run_events(updated)[0]["token_usage"] == CONFIRMED_USAGE


def test_technical_analysis_retry_reuses_existing_delivery_children(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "New onboarding", status="technical_analysis")

    workflow = load_workflow("discovery")
    technical_analysis = workflow.by_id["technical_analysis"]
    details = """Рекомендуем продолжить.

```yaml
implementation_required: true
delivery_tickets:
  - type: story
    title: "Story onboarding shell"
    description: Собрать базовый сценарий онбординга.
    priority: 20
    mandatory: true
  - type: task
    title: "Task analytics events"
    description: Добавить события для воронки.
    priority: 30
    mandatory: false
```
"""

    idea.active_run = "run-ta-1"
    orchestrator.store.save(idea)
    orchestrator._apply_result(
        workflow,
        idea.id,
        technical_analysis,
        AgentResult(outcome="needs_correction", summary="Нужно уточнение", details="Добавьте ограничения по ролям."),
    )

    corrected = orchestrator.store.get(idea.id)
    correction_children = orchestrator.store.children_of(idea.id, process="discovery")

    assert corrected.status == "technical_analysis"
    assert len(correction_children) == 1
    assert correction_children[0].type == "correction"

    corrected.active_run = "run-ta-2"
    orchestrator.store.save(corrected)
    orchestrator._apply_result(
        workflow,
        idea.id,
        technical_analysis,
        AgentResult(outcome="completed", summary="Готово", details=details),
    )

    first_children = orchestrator.store.children_of(idea.id, process="delivery")
    first_child_ids = [child.id for child in first_children]

    assert [(child.type, child.title) for child in first_children] == [
        ("story", "Story onboarding shell"),
        ("task", "Task analytics events"),
    ]
    assert [child.mandatory for child in first_children] == [True, False]

    completed = orchestrator.store.get(idea.id)
    completed.status = "technical_analysis"
    completed.active_run = "run-ta-3"
    orchestrator.store.save(completed)
    orchestrator._apply_result(
        workflow,
        idea.id,
        technical_analysis,
        AgentResult(outcome="completed", summary="Готово повторно", details=details),
    )

    second_children = orchestrator.store.children_of(idea.id, process="delivery")

    assert [child.id for child in second_children] == first_child_ids
    assert len(second_children) == 2
    assert all(child.status == "todo" for child in second_children)
    assert [child.mandatory for child in second_children] == [True, False]


@pytest.mark.parametrize("session_status", ["draft", "active"])
def test_technical_analysis_retry_preserves_selected_child_in_open_session(tmp_path: Path, session_status: str):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Session-aware onboarding", status="technical_analysis")
    child = orchestrator.store.create(
        "delivery", "story", "Story onboarding shell", parent=idea.id, status="selected_for_session"
    )
    session = orchestrator.session_store.create()
    orchestrator.session_store.add_ticket(session, child.id)
    if session_status == "active":
        orchestrator.session_store.activate(session)

    details = """```yaml
implementation_required: true
delivery_tickets:
  - type: story
    title: Story onboarding shell
    description: Собрать базовый сценарий онбординга.
```"""
    idea.active_run = "run-ta"
    orchestrator.store.save(idea)

    orchestrator._apply_result(
        load_workflow("discovery"),
        idea.id,
        load_workflow("discovery").by_id["technical_analysis"],
        AgentResult(outcome="completed", summary="Готово", details=details),
    )

    assert orchestrator.store.get(child.id).status == "selected_for_session"


def test_technical_analysis_retry_moves_legacy_selected_child_back_to_todo(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Legacy onboarding", status="technical_analysis")
    child = orchestrator.store.create(
        "delivery", "story", "Story onboarding shell", parent=idea.id, status="selected_for_session"
    )
    details = """```yaml
implementation_required: true
delivery_tickets:
  - type: story
    title: Story onboarding shell
```"""
    idea.active_run = "run-ta"
    orchestrator.store.save(idea)

    orchestrator._apply_result(
        load_workflow("discovery"),
        idea.id,
        load_workflow("discovery").by_id["technical_analysis"],
        AgentResult(outcome="completed", summary="Готово", details=details),
    )

    assert orchestrator.store.get(child.id).status == "todo"


def test_technical_analysis_retry_removes_stale_delivery_children_from_implementation(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "New onboarding", status="technical_analysis")

    workflow = load_workflow("discovery")
    technical_analysis = workflow.by_id["technical_analysis"]
    initial_details = """Рекомендуем продолжить.

```yaml
implementation_required: true
delivery_tickets:
  - type: story
    title: "Story onboarding shell"
    description: Собрать базовый сценарий онбординга.
    mandatory: true
  - type: task
    title: "Task analytics events"
    description: Добавить события для воронки.
    mandatory: true
```
"""
    revised_details = """Рекомендуем продолжить.

```yaml
implementation_required: true
delivery_tickets:
  - type: story
    title: "Story onboarding shell"
    description: Собрать базовый сценарий онбординга.
    mandatory: true
```
"""

    idea.active_run = "run-ta-1"
    orchestrator.store.save(idea)
    orchestrator._apply_result(
        workflow,
        idea.id,
        technical_analysis,
        AgentResult(outcome="completed", summary="Готово", details=initial_details),
    )

    initial_children = orchestrator.store.children_of(idea.id, process="delivery")
    assert [(child.type, child.title) for child in initial_children] == [
        ("story", "Story onboarding shell"),
        ("task", "Task analytics events"),
    ]

    idea = orchestrator.store.get(idea.id)
    idea.status = "technical_analysis"
    idea.active_run = "run-ta-2"
    orchestrator.store.save(idea)
    orchestrator._apply_result(
        workflow,
        idea.id,
        technical_analysis,
        AgentResult(outcome="completed", summary="Готово повторно", details=revised_details),
    )

    linked_children = orchestrator.store.children_of(idea.id, process="delivery")
    stale_child = orchestrator.store.get(initial_children[1].id)

    assert [(child.type, child.title) for child in linked_children] == [("story", "Story onboarding shell")]
    assert stale_child.parent is None
    assert stale_child.status == "done"

    delivery_workflow = load_workflow("delivery")
    delivery_candidates = select_candidates(delivery_workflow, orchestrator.store.list("delivery"), set())
    assert stale_child.id not in {candidate.ticket.id for candidate in delivery_candidates}

    idea = orchestrator.store.get(idea.id)
    idea.status = "implementation"
    orchestrator.store.save(idea)
    orchestrator._reconcile_tickets()

    assert orchestrator.store.get(idea.id).status == "implementation"

    story = orchestrator.store.get(initial_children[0].id)
    story.status = "done"
    orchestrator.store.save(story)
    orchestrator._reconcile_tickets()

    assert orchestrator.store.get(idea.id).status == "ready_for_validation"


def test_implementation_waits_only_for_mandatory_delivery_children(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Partial rollout", status="implementation")
    required = orchestrator.store.create("delivery", "story", "Required work", parent=idea.id, status="done", mandatory=True)
    optional = orchestrator.store.create("delivery", "task", "Optional work", parent=idea.id, status="review", mandatory=False)

    orchestrator._reconcile_tickets()

    idea = orchestrator.store.get(idea.id)
    required = orchestrator.store.get(required.id)
    optional = orchestrator.store.get(optional.id)

    assert required.mandatory is True
    assert optional.mandatory is False
    assert idea.status == "ready_for_validation"


def test_implementation_without_delivery_children_advances_to_validation(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "No delivery split", status="implementation")
    idea.implementation_required = True
    orchestrator.store.save(idea)

    orchestrator._reconcile_tickets()

    assert orchestrator.store.get(idea.id).status == "ready_for_validation"


def test_implementation_marked_unnecessary_ignores_stale_delivery_children(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "No longer needs delivery", status="implementation")
    idea.implementation_required = False
    orchestrator.store.save(idea)
    orchestrator.store.create("delivery", "story", "Stale work", parent=idea.id, status="review", mandatory=True)

    orchestrator._reconcile_tickets()

    assert orchestrator.store.get(idea.id).status == "ready_for_validation"


@pytest.mark.parametrize("parent_stage", ["analysis", "technical_analysis"])
def test_analysis_needs_correction_creates_blocking_child(tmp_path: Path, parent_stage: str):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Clarify scope", status=parent_stage)
    idea.active_run = f"run-{parent_stage}"
    orchestrator.store.save(idea)

    workflow = load_workflow("discovery")
    orchestrator._apply_result(
        workflow,
        idea.id,
        workflow.by_id[parent_stage],
        AgentResult(outcome="needs_correction", summary="Не хватает требований", details="Нужны ограничения по ролям."),
    )

    idea = orchestrator.store.get(idea.id)
    children = orchestrator.store.children_of(idea.id, process="discovery")

    assert idea.status == parent_stage
    assert len(children) == 1
    assert children[0].type == "correction"
    expected_status = "ready_for_analysis" if parent_stage == "analysis" else "ready_for_technical_analysis"
    assert children[0].status == expected_status
    assert children[0].correction_stage == parent_stage
    assert idea.blocked_by == [children[0].id]
    assert [entry["event"] for entry in run_events(idea)] == ["completed"]
    assert run_events(idea)[0]["run_id"] == f"run-{parent_stage}"

    correction = children[0]
    correction.status = "done"
    orchestrator.store.save(correction)
    orchestrator._reconcile_tickets()

    idea = orchestrator.store.get(idea.id)
    candidates = select_candidates(workflow, orchestrator.store.list("discovery"), set())

    assert idea.blocked_by == []
    assert [(candidate.ticket.id, candidate.target_status) for candidate in candidates if candidate.ticket.id == idea.id] == [
        (idea.id, parent_stage)
    ]


def test_identical_correction_is_not_created_twice(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Clarify scope", status="analysis")
    workflow = load_workflow("discovery")
    result = AgentResult(outcome="needs_correction", summary="Не хватает требований", details="Нужны ограничения по ролям.")

    idea.active_run = "run-1"
    orchestrator.store.save(idea)
    orchestrator._apply_result(workflow, idea.id, workflow.by_id["analysis"], result)
    correction = orchestrator.store.children_of(idea.id, process="discovery")[0]
    correction.status = "done"
    orchestrator.store.save(correction)
    orchestrator._reconcile_tickets()

    idea = orchestrator.store.get(idea.id)
    idea.active_run = "run-2"
    orchestrator.store.save(idea)
    orchestrator._apply_result(workflow, idea.id, workflow.by_id["analysis"], result)

    children = orchestrator.store.children_of(idea.id, process="discovery")
    assert [child.id for child in children] == [correction.id]


def test_correction_retries_origin_stage_without_nested_child_and_closes_on_success(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    parent = orchestrator.store.create("discovery", "idea", "Clarify scope", status="technical_analysis")
    parent.active_run = "run-parent"
    orchestrator.store.save(parent)
    workflow = load_workflow("discovery")
    stage = workflow.by_id["technical_analysis"]

    orchestrator._apply_result(
        workflow,
        parent.id,
        stage,
        AgentResult(outcome="needs_correction", summary="Нужна коррекция", details="Уточнить границы."),
    )
    correction = orchestrator.store.children_of(parent.id, process="discovery")[0]
    assert correction.status == "ready_for_technical_analysis"

    correction.status = "technical_analysis"
    correction.active_run = "run-correction"
    orchestrator.store.save(correction)
    orchestrator._apply_result(
        workflow,
        correction.id,
        stage,
        AgentResult(outcome="needs_correction", summary="Ещё уточнение", details="Но без дочернего тикета."),
    )
    correction = orchestrator.store.get(correction.id)
    assert correction.status == "technical_analysis"
    assert orchestrator.store.children_of(correction.id, process="discovery") == []

    correction.active_run = "run-correction-success"
    orchestrator.store.save(correction)
    orchestrator._apply_result(
        workflow,
        correction.id,
        stage,
        AgentResult(
            outcome="completed",
            summary="Коррекция принята",
            details="""```yaml
implementation_required: false
delivery_tickets: []
```""",
        ),
    )
    assert orchestrator.store.get(correction.id).status == "done"
    assert orchestrator.store.get(parent.id).blocked_by == []


def test_correction_technical_analysis_does_not_create_delivery_children(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    parent = orchestrator.store.create("discovery", "idea", "Group delivery work", status="analysis")
    correction = orchestrator.store.create(
        "discovery",
        "correction",
        "Clarify delivery group",
        parent=parent.id,
        status="technical_analysis",
        correction_stage="technical_analysis",
    )
    correction.active_run = "run-correction"
    orchestrator.store.save(correction)
    workflow = load_workflow("discovery")

    orchestrator._apply_result(
        workflow,
        correction.id,
        workflow.by_id["technical_analysis"],
        AgentResult(
            outcome="completed",
            summary="Коррекция принята",
            details="""```yaml
implementation_required: true
delivery_tickets:
  - type: task
    title: "Session model"
    description: "Define the session model."
    mandatory: true
```""",
        ),
    )

    assert orchestrator.store.get(correction.id).status == "done"
    assert orchestrator.store.children_of(correction.id, process="delivery") == []


def test_deferred_technical_debt_is_not_scheduled_without_active_session(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = CapturingRunner()
    source = orchestrator.store.create("discovery", "idea", "Source", status="technical_analysis")
    candidate = {
        "problem": "Deferred scheduling gap",
        "impact": "Unexpected launch",
        "suggested_scope": "Add scheduler guard",
        "evidence": {"path": "README.md", "identifier": "Scheduler", "observation": "Observed"},
    }
    key, basis = technical_debt_basis({**candidate, "source_ticket": source.id}, tmp_path)
    result = TicketWriteService(tmp_path).create_technical_debt_ticket(
        **candidate,
        source_ticket=source.id,
        source_stage="technical_analysis",
        source_run="run-source",
        dedup_key=key,
        dedup_basis=basis,
        priority=7,
        origin="technical_analysis:run-source",
        actor="test",
    )
    assert result.ticket is not None
    debt = result.ticket
    debt.status = "selected_for_session"
    orchestrator.store.save(debt)

    asyncio.run(orchestrator._schedule_once())

    scheduled = orchestrator.store.get(debt.id)
    assert scheduled.status == "selected_for_session"
    assert scheduled.active_run is None
    assert [entry["event"] for entry in scheduled.run_history] == ["created"]
    assert orchestrator.runner.contracts == []


def test_deferred_technical_debt_is_non_blocking_for_other_delivery_work(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = CapturingRunner()
    orchestrator.worker_control.set_limit(1)

    debt = orchestrator.store.create("delivery", "task", "Deferred debt", status="selected_for_session")
    debt.technical_debt_deferred = True
    orchestrator.store.save(debt)
    regular = orchestrator.store.create("delivery", "task", "Regular work", status="selected_for_session")

    asyncio.run(orchestrator._schedule_once())

    assert orchestrator.store.get(debt.id).status == "selected_for_session"
    assert orchestrator.store.get(debt.id).active_run is None
    regular_after = orchestrator.store.get(regular.id)
    assert [contract.run_id for contract in orchestrator.runner.contracts] == [
        entry["run_id"] for entry in regular_after.run_history if entry["event"] == "started"
    ]
    assert regular_after.status != "selected_for_session"
