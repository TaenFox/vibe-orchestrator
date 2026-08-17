import asyncio
from pathlib import Path

from vibe_orchestrator.codex import AgentResult, ExecutionContract
from vibe_orchestrator.config import PromptSpec, load_workflow
from vibe_orchestrator.orchestrator import Orchestrator
from vibe_orchestrator.scheduler import select_candidates
from vibe_orchestrator.tickets import next_status_for_ticket


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


class FailingRunner:
    def __init__(self):
        self.profile = {"model": "gpt-5.4", "reasoning_effort": "high"}
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
    assert children[0].wip_exempt is True
    assert [entry["event"] for entry in parent.run_history] == ["completed"]
    assert all(entry["run_id"] == "run-review" for entry in parent.run_history)


def test_schedule_records_started_and_completed_run_history(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = SuccessfulRunner()
    ticket = orchestrator.store.create("delivery", "task", "Ship durable history", status="ready_for_review")
    ticket.last_outcome = "needs_rework"
    ticket.last_summary = "Previous review failed"
    orchestrator.store.save(ticket)

    asyncio.run(_schedule_and_wait(orchestrator, ticket.id))
    orchestrator._reap_finished()

    ticket = orchestrator.store.get(ticket.id)

    assert ticket.active_run is None
    assert ticket.last_outcome == "completed"
    assert ticket.last_summary == "review done"
    assert ticket.status == "ready_for_acceptance"
    assert [entry["event"] for entry in ticket.run_history] == ["started", "completed"]
    assert ticket.run_history[0]["run_id"] == ticket.run_history[1]["run_id"]
    assert ticket.run_history[0]["event"] == "started"
    assert ticket.run_history[0]["artifacts_path"].endswith(ticket.run_history[0]["run_id"])
    assert ticket.run_history[0]["prompt_path"] == "delivery/review.md"
    assert ticket.run_history[0]["prompt_version"] == "sha256:stub"
    assert ticket.run_history[0]["model"] == "gpt-5.4"
    assert ticket.run_history[0]["reasoning_effort"] == "low"
    assert ticket.run_history[0]["ticket_title"] == "Ship durable history"
    assert ticket.run_history[0]["ticket_priority"] == "100"
    assert ticket.run_history[0]["ticket_parent"] == "none"
    assert ticket.run_history[0]["ticket_description"] == "(пусто)"
    assert ticket.run_history[1]["to_status"] == "ready_for_acceptance"
    assert ticket.run_history[1]["ticket_title"] == "Ship durable history"
    assert ticket.run_history[1]["ticket_priority"] == "100"


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
    assert [entry["event"] for entry in ticket.run_history] == ["started", "failed"]
    assert ticket.run_history[-1]["summary"] == "agent crashed"
    assert ticket.run_history[-1]["prompt_path"] == "delivery/review.md"
    assert ticket.run_history[-1]["prompt_version"] == "sha256:stub"
    assert ticket.run_history[-1]["artifacts_path"] == ".vibe/runs/run-failed"


def test_schedule_preserves_execution_contract_when_prompt_changes_during_run(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = MutablePromptRunner()
    ticket = orchestrator.store.create("delivery", "task", "Ship durable history", status="ready_for_review")

    asyncio.run(_schedule_and_wait(orchestrator, ticket.id))

    ticket = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in ticket.run_history] == ["started", "completed"]
    assert [entry["prompt_version"] for entry in ticket.run_history] == ["sha256:initial", "sha256:initial"]
    assert [entry["prompt_path"] for entry in ticket.run_history] == ["delivery/review.md", "delivery/review.md"]


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

    assert [entry["event"] for entry in reloaded.run_history] == ["started", "completed"]
    assert all(entry["ticket_title"] == "Original title" for entry in reloaded.run_history)
    assert all(entry["ticket_priority"] == "42" for entry in reloaded.run_history)
    assert all(entry["ticket_parent"] == "DEL-PARENT" for entry in reloaded.run_history)
    assert all(entry["ticket_description"] == "Original description" for entry in reloaded.run_history)


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
        assert [entry["event"] for entry in scheduled.run_history] == ["started"]

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
        model="gpt-5.4",
        reasoning_effort="high",
    )
    orchestrator.store.save(ticket)

    asyncio.run(orchestrator._execute(load_workflow("delivery"), ticket.id, "review", "run-broken"))

    ticket = orchestrator.store.get(ticket.id)

    assert ticket.active_run is None
    assert ticket.last_outcome == "failed"
    assert ticket.last_summary == "prompt file disappeared"
    assert [entry["event"] for entry in ticket.run_history] == ["started", "failed"]
    assert ticket.run_history[-1]["summary"] == "prompt file disappeared"
    assert ticket.run_history[-1]["model"] == "gpt-5.4"
    assert ticket.run_history[-1]["reasoning_effort"] == "high"
    assert "prompt_path" not in ticket.run_history[-1]
    assert "prompt_version" not in ticket.run_history[-1]


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
        model="gpt-5.4",
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

    assert [entry["event"] for entry in ticket.run_history] == ["started", "completed"]
    assert ticket.run_history[-1]["model"] == "gpt-5.4"
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
        model="gpt-5.4",
        reasoning_effort="high",
    )
    orchestrator.store.save(ticket)
    runner.profile = {"model": "drifted-model", "reasoning_effort": "low"}
    runner.prompt = PromptSpec(path="delivery/review-v2.md", body="", version="sha256:drifted")

    asyncio.run(orchestrator._execute(load_workflow("delivery"), ticket.id, "review", "run-failed-drift"))

    ticket = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in ticket.run_history] == ["started", "failed"]
    assert ticket.run_history[-1]["model"] == "gpt-5.4"
    assert ticket.run_history[-1]["reasoning_effort"] == "high"
    assert ticket.run_history[-1]["prompt_path"] == "delivery/review.md"
    assert ticket.run_history[-1]["prompt_version"] == "sha256:stable"


def test_failed_run_history_preserves_execution_contract_when_prompt_changes_during_run(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.runner = MutableFailingRunner()
    ticket = orchestrator.store.create("delivery", "task", "Freeze failure metadata", status="ready_for_review")

    asyncio.run(_schedule_and_wait(orchestrator, ticket.id))

    ticket = orchestrator.store.get(ticket.id)

    assert [entry["event"] for entry in ticket.run_history] == ["started", "failed"]
    assert [entry["prompt_version"] for entry in ticket.run_history] == ["sha256:stub", "sha256:stub"]
    assert [entry["prompt_path"] for entry in ticket.run_history] == ["delivery/review.md", "delivery/review.md"]


def test_completed_rework_unblocks_parent(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    parent = orchestrator.store.create("delivery", "task", "Parent task", status="review")
    child = orchestrator.store.create("delivery", "rework", "Child rework", parent=parent.id, status="acceptance")
    parent.blocked_by = [child.id]
    orchestrator.store.save(parent)
    child.active_run = "run-acceptance"
    orchestrator.store.save(child)

    workflow = load_workflow("delivery")
    orchestrator._apply_result(
        workflow,
        child.id,
        workflow.by_id["acceptance"],
        AgentResult(outcome="completed", summary="Исправление принято", details=""),
    )

    parent = orchestrator.store.get(parent.id)
    child = orchestrator.store.get(child.id)

    assert child.status == "ready_for_release"
    assert not orchestrator.store.is_done(child)
    assert parent.blocked_by == [child.id]

    child.status = "done"
    orchestrator.store.save(child)
    orchestrator._reconcile_tickets()

    parent = orchestrator.store.get(parent.id)
    child = orchestrator.store.get(child.id)

    assert child.status == "done"
    assert parent.blocked_by == []
    assert [entry["event"] for entry in child.run_history] == ["completed"]
    assert child.run_history[0]["run_id"] == "run-acceptance"


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
    assert all(child.status == "selected_for_session" for child in children)
    assert all(child.mandatory is True for child in children)
    assert idea.implementation_required is True
    assert next_status_for_ticket(orchestrator.store, idea) == "implementation"
    assert [entry["event"] for entry in idea.run_history] == ["completed"]
    assert idea.run_history[0]["run_id"] == "run-ta"

    idea.status = "implementation"
    orchestrator.store.save(idea)
    orchestrator._reconcile_tickets()
    assert orchestrator.store.get(idea.id).status == "implementation"

    for child in children:
        child.status = "done"
        orchestrator.store.save(child)
    orchestrator._reconcile_tickets()

    assert orchestrator.store.get(idea.id).status == "ready_for_validation"


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
    assert all(child.status == "selected_for_session" for child in second_children)
    assert [child.mandatory for child in second_children] == [True, False]


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


def test_analysis_needs_correction_creates_blocking_child(tmp_path: Path):
    orchestrator = Orchestrator(tmp_path)
    idea = orchestrator.store.create("discovery", "idea", "Clarify scope", status="analysis")
    idea.active_run = "run-analysis"
    orchestrator.store.save(idea)

    workflow = load_workflow("discovery")
    orchestrator._apply_result(
        workflow,
        idea.id,
        workflow.by_id["analysis"],
        AgentResult(outcome="needs_correction", summary="Не хватает требований", details="Нужны ограничения по ролям."),
    )

    idea = orchestrator.store.get(idea.id)
    children = orchestrator.store.children_of(idea.id, process="discovery")

    assert idea.status == "analysis"
    assert len(children) == 1
    assert children[0].type == "correction"
    assert children[0].status == "ready"
    assert idea.blocked_by == [children[0].id]
    assert [entry["event"] for entry in idea.run_history] == ["completed"]
    assert idea.run_history[0]["run_id"] == "run-analysis"
