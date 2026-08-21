from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .codex import AgentResult, CodexRunner, ExecutionContract, ticket_prompt_metadata
from .environment import ProjectEnvironment
from .config import Stage, Workflow, load_all_workflows
from .control import WorkerControl
from .git_trees import GitTreeError, GitTreeManager
from .scheduler import select_candidates
from .tickets import RETRY_BACKOFF_SECONDS, Ticket, TicketStore, TicketWriteService
from .sessions import SessionStore
from .technical_debt import ObservationVerifier, TechnicalDebtError, parse_technical_debt, preflight_technical_debt, technical_debt_basis
from .token_usage import is_confirmed_token_usage, unknown_token_usage
from .budget_ledger import BudgetDenied, BudgetLedger, TERMINAL
from .run_store import RunStore
from .human_gate import validate_gate, resolve_gate

log = logging.getLogger("vibe")
MAX_REWORK_REVIEW_ATTEMPTS = 3
STALE_RUN_TIMEOUT = timedelta(minutes=30)


def resume_rework(store: TicketStore, ticket_id: str) -> Ticket:
    """Authorize one explicit corrective pass after the cycle guard fired."""
    ticket = store.get(ticket_id)
    if ticket.process != "delivery" or ticket.type != "rework":
        raise ValueError("Возобновить можно только Delivery rework")
    if ticket.blocked_reason != "rework_cycle_stopped":
        raise ValueError("Тикет не ожидает ручного разрешения реворка")
    # A resumed rework must be re-analyzed before implementation. This keeps
    # the corrective scope explicit instead of sending review findings straight
    # back to development.
    ticket.status = "selected_for_session"
    ticket.blocked_reason = None
    ticket.last_outcome = "manual_rework_resumed"
    ticket.last_summary = "Ручное разрешение: запущен дополнительный проход реворка"
    store.record_run_event(
        ticket,
        run_id=None,
        stage_id=ticket.status,
        event="manual_rework_resumed",
        reason="rework_cycle_stopped",
        to_status=ticket.status,
    )
    store.save(ticket)
    return ticket


def recover_stale_run(store: TicketStore, ticket_id: str, *, now: datetime | None = None,
                      timeout: timedelta = STALE_RUN_TIMEOUT, force: bool = False) -> Ticket:
    """Recover a started run; stale runs are automatic, fresh runs require force."""
    ticket = store.get(ticket_id)
    run_id = ticket.active_run
    if not run_id:
        raise ValueError("У тикета нет активного запуска")
    run = RunStore(store.database).get(run_id)
    if not run or run.get("state") != "started":
        raise ValueError("Активный запуск уже завершён или не найден")
    started_at = datetime.fromisoformat(run["started_at"])
    current = now or datetime.now(timezone.utc)
    age = current - started_at
    if age < timeout and not force:
        raise ValueError(f"Запуск ещё не считается зависшим: {int(age.total_seconds())} секунд")
    started_events = [event for event in ticket.run_history if event.get("run_id") == run_id and event.get("event") == "started"]
    source_status = started_events[-1].get("from_status") if started_events else None
    if not source_status:
        raise ValueError("Не найден исходный статус зависшего запуска")
    if not RunStore(store.database).abort(run_id, reason="stale_run_recovered"):
        raise ValueError("Запуск уже изменён другим процессом")
    ticket.active_run = None
    ticket.status = source_status
    ticket.last_outcome = "run_interrupted"
    ticket.last_summary = f"Зависший запуск восстановлен после {int(age.total_seconds() // 60)} мин.; тикет возвращён в очередь"
    store.record_run_event(ticket, run_id=run_id, stage_id=source_status, event="stale_run_recovered",
                           reason="stale_run_recovered", age_seconds=int(age.total_seconds()), to_status=source_status)
    store.save(ticket)
    return ticket


def decide_human_gate(store: TicketStore, ticket_id: str, decision: str, *, actor: str = "owner") -> Ticket:
    ticket = store.get(ticket_id)
    gate = ticket.context.get("human_gate") if isinstance(ticket.context, dict) else None
    stage_id = gate.get("stage") if isinstance(gate, dict) else None
    if not isinstance(stage_id, str):
        raise ValueError("В human_gate отсутствует этап для продолжения")
    workflow = load_all_workflows()[ticket.process]
    queue = next((item.id for item in workflow.stages if item.kind == "queue" and item.pull_to == stage_id), None)
    if queue is None:
        raise ValueError(f"Для этапа {stage_id} не найдена очередь продолжения")
    resolve_gate(ticket, decision, actor=actor)
    ticket.status = queue
    store.record_run_event(ticket, run_id=None, stage_id=queue, event="human_decision", decision=decision, actor=actor, to_status=queue)
    store.save(ticket)
    return ticket


class Orchestrator:
    def __init__(
        self,
        project: Path,
        poll_interval: float = 2.0,
        max_agents: int | None = None,
        observation_verifier: ObservationVerifier | None = None,
    ):
        self.store = TicketStore(project)
        self.store.init()
        self.workflows = load_all_workflows()
        self.runner = CodexRunner(self.store)
        self.poll_interval = poll_interval
        self.worker_control = WorkerControl(project)
        self.session_store = SessionStore(project, self.store)
        initial_worker_limit = self.worker_control.get_limit() if max_agents is None else max_agents
        self.worker_control.set_limit(initial_worker_limit)
        self.max_agents = initial_worker_limit
        self._last_worker_limit = initial_worker_limit
        self.observation_verifier = observation_verifier
        self.tree_manager = GitTreeManager(project, self.store)
        self.environment = ProjectEnvironment(project)
        self.environment.ensure()
        self.ledger = BudgetLedger(project)
        self.running: dict[str, asyncio.Task[None]] = {}

    async def run_forever(self) -> None:
        if not self.runner.available():
            raise RuntimeError("Codex CLI не найден в PATH. Установите Codex и выполните вход перед запуском оркестратора.")
        log.info("наблюдение за %s", self.store.project)
        while True:
            self._reap_finished()
            self._reconcile_tickets()
            self.ledger.reconcile()
            await self._schedule_once()
            await asyncio.sleep(self.poll_interval)

    def _materialize_delivery_membership(self) -> tuple[list[Any], dict[str, Any], tuple[Any, ...]]:
        """Read active sessions once and derive all polling membership views."""
        sessions = [session for session in self.session_store.list() if session.status == "active"]
        session_by_ticket: dict[str, Any] = {}
        effective_by_session: dict[str, tuple[str, ...]] = {}
        for session in sessions:
            effective_ids = self.session_store.effective_ticket_id_sequence(session)
            effective_by_session[session.id] = effective_ids
            for ticket_id in effective_ids:
                session_by_ticket.setdefault(ticket_id, session)
        snapshot = tuple(
            (session.id, session.updated_at, effective_by_session[session.id])
            for session in sessions
        )
        return sessions, session_by_ticket, snapshot

    async def _schedule_once(self) -> None:
        worker_limit = self._read_worker_limit()
        slots = worker_limit - len(self.running)
        if slots <= 0:
            return
        global_candidates = []
        running_ids = set(self.running)
        # Session writers use the same lock. Materialize the map and its
        # token in one critical section so an update cannot split the two
        # reads that define this polling snapshot.
        with self.session_store.admission_lock():
            active_delivery_sessions, session_by_ticket, membership_snapshot = (
                self._materialize_delivery_membership()
            )
        delivery_session_participants = set(session_by_ticket)
        if not active_delivery_sessions:
            delivery_session_participants = None
        for process, workflow in self.workflows.items():
            tickets = self.store.list(process)
            session_participants = delivery_session_participants if process == "delivery" else None
            global_candidates.extend(
                (workflow, c)
                for c in select_candidates(workflow, tickets, running_ids, session_participants=session_participants)
            )
        global_candidates.sort(
            key=lambda item: (
                -item[1].stage_position,
                0 if item[1].ticket.wip_exempt else 1,
                item[1].ticket.priority,
                item[1].ticket.created_at,
                item[1].ticket.id,
            )
        )
        for workflow, candidate in global_candidates[:slots]:
            if len(self.running) >= self._read_worker_limit():
                break
            ticket = self.store.get(candidate.ticket.id)
            if ticket.active_run or ticket.blocked_by or ticket.status != candidate.source_status:
                continue
            stage = self._stage_for_ticket(workflow, workflow.by_id[candidate.target_status], ticket)
            session = session_by_ticket.get(ticket.id)
            if workflow.id == "delivery" and active_delivery_sessions and session is None:
                ticket.blocked_reason = "session_membership_required"
                ticket.last_outcome = "blocked_budget"
                ticket.last_summary = "Тикет не включен в активную Delivery-сессию"
                self.store.save(ticket)
                continue
            run_id = uuid.uuid4().hex
            workspace = None
            try:
                contract = self.runner.prepare_execution_contract(stage, run_id)
            except Exception as exc:
                try:
                    metadata = self.runner.execution_profile(stage)
                except Exception:
                    metadata = {}
                if stage.prompt:
                    metadata["prompt_path"] = stage.prompt
                ticket.status = candidate.target_status
                ticket.active_run = run_id
                ticket.retry_after = None
                self.store.record_run_event(
                    ticket,
                    run_id=run_id,
                    stage_id=candidate.target_status,
                    event="started",
                    from_status=candidate.source_status,
                    to_status=candidate.target_status,
                    context_revision=ticket.context_revision,
                    **metadata,
                    **ticket_prompt_metadata(ticket),
                )
                self.store.save(ticket)
                self._record_failure(ticket, run_id, candidate.target_status, exc, metadata)
                log.exception("сбой подготовки запуска для %s (%s)", ticket.id, ticket.type)
                continue
            session_id = session.id if session else None
            attempt_kind = "rework" if ticket.type == "rework" else "initial"
            try:
                # Session writers and this revalidation share sessions.lock.
                # A membership update therefore commits either before the
                # fresh snapshot (and is detected) or after reservation.
                with self.session_store.admission_lock():
                    fresh_sessions, fresh_by_ticket, fresh_snapshot = self._materialize_delivery_membership()
                    # The candidate and its session must come from the same
                    # polling snapshot.  This also covers removal of the last
                    # active session (fresh_sessions == []).
                    if fresh_snapshot != membership_snapshot:
                        continue
                    fresh_session = fresh_by_ticket.get(ticket.id)
                    if workflow.id == "delivery" and active_delivery_sessions and fresh_session is None:
                        continue
                    if session and (fresh_session is None or fresh_session.id != session.id or
                                     fresh_session.updated_at != session.updated_at):
                        continue
                    fresh_ticket = self.store.get(ticket.id)
                    if (fresh_ticket.active_run or fresh_ticket.blocked_by or
                            fresh_ticket.status != candidate.source_status):
                        continue
                    ticket = fresh_ticket
                    session = fresh_session if workflow.id == "delivery" else session
                    session_id = session.id if session else None
                    reservation = self.ledger.reserve(
                        contract.run_id, ticket.id, session_id, self._planned_budget(ticket),
                        attempt_kind=attempt_kind,
                        parent_ticket_id=ticket.parent,
                        budget_owner_ticket_id=ticket.parent if attempt_kind == "rework" else ticket.id,
                        require_session_budget=bool(session and session.budget_policy == "enforced"),
                    )
                    # Claim the ticket before releasing the shared admission
                    # lock. A concurrent scheduler therefore observes the
                    # active_run and cannot reserve/start a second run.
                    ticket.status = candidate.target_status
                    ticket.active_run = contract.run_id
                    ticket.retry_after = None
                    ticket.blocked_reason = None
                    self.store.record_run_event(
                        ticket,
                        run_id=contract.run_id,
                        stage_id=candidate.target_status,
                        event="started",
                        from_status=candidate.source_status,
                        to_status=candidate.target_status,
                        context_revision=ticket.context_revision,
                        reservation=self._reservation_metadata(reservation.run_id),
                        **contract.history_metadata(),
                        **ticket_prompt_metadata(ticket),
                    )
                    self.store.save(ticket)
            except BudgetDenied as exc:
                reason_code = getattr(exc, "reason_code", "budget_denied")
                already_blocked = ticket.last_outcome == "blocked_budget" and ticket.blocked_reason == reason_code
                ticket.blocked_reason = reason_code
                ticket.last_outcome = "blocked_budget"
                ticket.last_summary = str(exc)
                if not already_blocked:
                    self.store.record_run_event(
                        ticket,
                        run_id=None,
                        stage_id=candidate.target_status,
                        event="blocked",
                        reason_code=reason_code,
                        reason=str(exc),
                    )
                self.store.save(ticket)
                log.info("запуск %s отклонен budget gate: %s", ticket.id, reason_code)
                continue
            contract = replace(contract, reservation_metadata=self._reservation_metadata(reservation.run_id))
            try:
                workspace = self.tree_manager.workspace_for(ticket, stage_id=stage.id)
            except Exception as exc:
                if not reservation.legacy:
                    self.ledger.release(contract.run_id)
                self._record_failure(ticket, contract.run_id, candidate.target_status, exc, contract.history_metadata())
                log.exception("сбой подготовки workspace для %s (%s)", ticket.id, ticket.type)
                continue
            execution = self._execute(workflow, ticket.id, candidate.target_status, contract, workspace=workspace)
            try:
                task = asyncio.create_task(execution, name=ticket.id)
            except Exception as exc:
                execution.close()
                if not reservation.legacy:
                    self.ledger.release(contract.run_id)
                self._record_failure(ticket, contract.run_id, candidate.target_status, exc, contract.history_metadata())
                log.exception("сбой создания task для %s (%s)", ticket.id, ticket.type)
                continue
            self.running[ticket.id] = task
            log.info("запущено %s (%s) -> %s", ticket.id, ticket.type, candidate.target_status)

    def _read_worker_limit(self) -> int:
        worker_limit = self.worker_control.get_limit(self._last_worker_limit)
        if worker_limit != self._last_worker_limit:
            log.info("лимит воркеров изменен: %s -> %s", self._last_worker_limit, worker_limit)
            self._last_worker_limit = worker_limit
        return worker_limit

    @staticmethod
    def _planned_budget(ticket: Ticket) -> dict[str, int | None]:
        budget = ticket.context.get("budget", {}) if isinstance(ticket.context, dict) else {}
        planned = budget.get("planned", {}) if isinstance(budget, dict) else {}
        return {"tokens": planned.get("tokens"), "points": planned.get("points"), "runs": planned.get("runs", 1)}

    def _reservation_metadata(self, run_id: str) -> dict[str, object]:
        run = self.ledger.get_run(run_id)
        if run is None:
            return {"contract_version": "budget.v1", "run_id": run_id}
        return {
            "contract_version": "budget.v1",
            "run_id": run["run_id"],
            "state": run["state"],
            "attempt_kind": run["attempt_kind"],
            "ticket_budget_id": run["ticket_budget_id"],
            "session_budget_id": run["session_budget_id"],
            "parent_run_id": run["parent_run_id"],
            "parent_ticket_id": run["parent_ticket_id"],
            "planned": run["planned"],
            "reserved": run["reserved"],
            "reserved_at": run["reserved_at"],
        }

    def _stage_for_ticket(self, workflow: Workflow, stage: Stage, ticket: Ticket) -> Stage:
        if workflow.id != "process_management" or stage.id != "in_progress":
            return stage
        prompt = {
            "audit": "process_management/audit.md",
            "planning": "process_management/planning.md",
            "estimation": "process_management/estimation.md",
        }.get(ticket.type)
        return replace(stage, prompt=prompt) if prompt else stage

    async def _execute(self, workflow: Workflow, ticket_id: str, stage_id: str, contract: ExecutionContract | str, *, workspace: Path | None = None) -> None:
        ticket = self.store.get(ticket_id)
        stage = self._stage_for_ticket(workflow, workflow.by_id[stage_id], ticket)
        metadata: dict[str, str] | None = None
        try:
            if isinstance(contract, str):
                metadata = self._run_traceability_metadata(ticket, contract, stage)
                contract = self.runner.prepare_execution_contract(stage, contract)
            else:
                metadata = contract.history_metadata()
            run_kwargs = {"contract": contract}
            if workspace is not None:
                run_kwargs["workspace"] = workspace
            if self.environment.agent_environment() is not None:
                run_kwargs["environment"] = self.environment.agent_environment()
                run_kwargs["environment_instructions"] = self.environment.instructions()
            ledger_run = self.ledger.get_run(contract.run_id)
            if ledger_run is not None:
                run_kwargs["on_process_started"] = lambda started_run_id: self.ledger.start(started_run_id)
            # Keep small test/delivery runner adapters source-compatible while
            # making the production runner own the subprocess boundary.
            parameters = inspect.signature(self.runner.run).parameters
            if "on_process_started" not in parameters and not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                run_kwargs.pop("on_process_started", None)
            if "environment" not in parameters and not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                run_kwargs.pop("environment", None)
            if "environment_instructions" not in parameters and not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                run_kwargs.pop("environment_instructions", None)
            result = await self.runner.run(ticket, stage, contract.run_id, **run_kwargs)
            ledger_run = self.ledger.get_run(contract.run_id)
            if ledger_run is not None and ledger_run["state"] == "reserved_pending_start":
                # Compatibility adapters may not expose the subprocess hook;
                # a returned result proves that execution passed its boundary.
                self.ledger.start(contract.run_id)
            self._apply_result(workflow, ticket_id, stage, result, contract=contract)
            log.info("завершено %s (%s): %s -> %s", ticket_id, self.store.get(ticket_id).type, result.outcome, self.store.get(ticket_id).status)
        except TechnicalDebtError as exc:
            # Contract rejection is deliberately not an agent failure: recording it
            # would mutate the source ticket and route it through correction flow.
            # Keep the envelope intact for the caller/operator and only release
            # bookkeeping that has not crossed the subprocess boundary.
            if not isinstance(contract, str):
                ledger_run = self.ledger.get_run(contract.run_id)
                if ledger_run is not None and ledger_run["state"] == "reserved_pending_start":
                    self.ledger.release(contract.run_id)
            log.error("отклонен tech_debt_candidates контракт для %s: %s", ticket_id, yaml.safe_dump(exc.envelope, allow_unicode=True, sort_keys=False))
        except Exception as exc:
            ticket = self.store.get(ticket_id)
            if isinstance(contract, str):
                run_id = contract
                metadata = metadata or self._run_traceability_metadata(ticket, run_id, stage, suppress_prompt_errors=True)
            else:
                run_id = contract.run_id
                metadata = metadata or self._run_traceability_metadata(ticket, run_id, stage, fallback=contract.history_metadata())
            ledger_run = self.ledger.get_run(run_id)
            if ledger_run is not None and ledger_run["state"] == "reserved_pending_start":
                self.ledger.release(run_id)
            self._record_failure(ticket, run_id, stage_id, exc, metadata)
            log.exception("сбой воркера для %s (%s)", ticket_id, ticket.type)

    def _record_failure(self, ticket: Ticket, run_id: str, stage_id: str, exc: Exception, metadata: dict[str, str]) -> None:
        ticket.consecutive_failures += 1
        retry_delay = (
            RETRY_BACKOFF_SECONDS[ticket.consecutive_failures - 1]
            if ticket.consecutive_failures <= len(RETRY_BACKOFF_SECONDS)
            else None
        )
        ticket.retry_after = (
            (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat()
            if retry_delay is not None
            else None
        )
        self.store.record_run_event(
            ticket,
            run_id=run_id,
            stage_id=stage_id,
            event="failed",
            outcome="failed",
            summary=str(exc),
            consecutive_failures=ticket.consecutive_failures,
            retry_after=ticket.retry_after,
            token_usage=self._run_token_usage(run_id),
            **metadata,
        )
        ticket.active_run = None
        ticket.last_outcome = "failed"
        ticket.last_summary = str(exc)
        self.store.save(ticket)
        self._ledger_finalize(run_id, "failed")

    def _ledger_finalize(self, run_id: str | None, outcome: str, usage: dict | None = None) -> None:
        run = self.ledger.get_run(run_id) if run_id else None
        if run is None or run["state"] in TERMINAL:
            return
        self.ledger.finalize(run_id, outcome if outcome in {"completed", "failed", "unknown"} else "failed", usage or self._run_token_usage(run_id))

    def _apply_result(
        self,
        workflow: Workflow,
        ticket_id: str,
        stage: Stage,
        result: AgentResult,
        *,
        run_id: str | None = None,
        contract: ExecutionContract | None = None,
    ) -> None:
        if result.outcome not in (stage.outcomes or {}):
            raise ValueError(f"Outcome {result.outcome!r} is not allowed for {workflow.id}/{stage.id}")
        if workflow.id == "discovery" and stage.id == "technical_analysis" and result.outcome == "completed":
            try:
                _technical_analysis_plan(result.details)
                debt_candidates = parse_technical_debt(result.details)
                if debt_candidates:
                    preflight_technical_debt(
                        debt_candidates,
                        project=self.store.project,
                        ticket_store=self.store,
                        session_store=self.session_store,
                        observation_verifier=self.observation_verifier,
                    )
            except TechnicalDebtError:
                raise
            except ValueError as exc:
                result = AgentResult(
                    outcome="needs_correction",
                    summary="Технический анализ вернул некорректный план реализации",
                    details=str(exc),
                    token_usage=result.token_usage,
                )
        ticket = self.store.get(ticket_id)
        active_run = contract.run_id if contract else run_id or ticket.active_run
        ticket.blocked_reason = None
        metadata = self._run_traceability_metadata(
            ticket,
            active_run,
            stage,
            fallback=contract.history_metadata() if contract else None,
        )
        ticket.last_outcome = result.outcome
        ticket.last_summary = result.summary
        ticket.consecutive_failures = 0
        ticket.retry_after = None
        context_before = ticket.context_revision
        context_update = _extract_context_payload(result.details)
        if result.outcome == "needs_human_decision":
            gate = validate_gate(context_update.get("human_gate"))
            gate["stage"] = stage.id
            gate["run_id"] = active_run
            gate["requested_at"] = datetime.now(timezone.utc).isoformat()
            context_update["human_gate"] = gate
        if context_update:
            ticket.context = _merge_context(ticket.context, context_update)
            ticket.context_revision += 1
        target_status = (stage.outcomes or {})[result.outcome]
        if result.outcome == "needs_human_decision":
            target_status = stage.id
            ticket.blocked_reason = "human_decision_required"
        if ticket.type == "rework" and workflow.id == "delivery" and result.outcome == "needs_rework":
            attempts = sum(
                1
                for event in ticket.run_history
                if event.get("stage") == stage.id
                and event.get("event") == "completed"
                and event.get("outcome") == "needs_rework"
            ) + 1
            if attempts < MAX_REWORK_REVIEW_ATTEMPTS:
                # Rework findings can change the scope. Always pass them through
                # system analysis before another implementation attempt.
                target_status = "selected_for_session"
            else:
                target_status = "selected_for_session"
                ticket.blocked_reason = "rework_cycle_stopped"
        if ticket.type == "correction" and workflow.id == "discovery" and result.outcome == "completed":
            target_status = "done"
        if ticket.type == "rework" and workflow.id == "delivery" and stage.id == ticket.rework_stage and result.outcome == "completed":
            target_status = "ready_for_release" if self.tree_manager.enabled() else "done"
        ticket.status = target_status
        if active_run:
            token_usage = result.token_usage or self._run_token_usage(active_run)
            if not is_confirmed_token_usage(
                token_usage,
                run_id=active_run,
                model=contract.model if contract else None,
                reasoning_effort=contract.reasoning_effort if contract else None,
            ):
                token_usage = unknown_token_usage(
                    run_id=active_run,
                    model=contract.model if contract else None,
                    reasoning_effort=contract.reasoning_effort if contract else None,
                )
            self.store.record_run_event(
                ticket,
                run_id=active_run,
                stage_id=stage.id,
                event="completed",
                outcome=result.outcome,
                summary=result.summary,
                to_status=ticket.status,
                context_revision_before=context_before,
                context_revision_after=ticket.context_revision,
                token_usage=token_usage,
                **metadata,
            )
            self._ledger_finalize(active_run, result.outcome, token_usage)
        ticket.active_run = None
        self._handle_follow_up(ticket, workflow, stage, result)
        self.store.save(ticket)
        self._release_parent_if_resolved(ticket)
        self._reconcile_tickets()

    def _run_token_usage(self, run_id: str | None) -> dict[str, object]:
        if not run_id:
            return unknown_token_usage()
        manifest_path = self.store.run_path(run_id) / "run.json"
        try:
            import json

            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return unknown_token_usage()
        usage = payload.get("token_usage")
        return usage if isinstance(usage, dict) else unknown_token_usage()

    def _handle_follow_up(self, ticket: Ticket, workflow: Workflow, stage: Stage, result: AgentResult) -> None:
        if workflow.id == "delivery" and result.outcome == "needs_rework":
            if ticket.type == "rework":
                # Rework remains on the same Delivery route instead of nesting another rework.
                return
            child = self._create_corrective_child(ticket, child_type="rework", status="selected_for_session", stage=stage, summary=result.summary, details=result.details)
            ticket.blocked_by = sorted({*ticket.blocked_by, child.id})
            return
        if workflow.id == "discovery" and result.outcome == "needs_correction":
            if ticket.type == "correction":
                # A correction retries its originating stage instead of creating a correction chain.
                return
            if stage.id == "technical_analysis":
                ticket.implementation_required = None
            child = self._create_corrective_child(ticket, child_type="correction", status="ready", stage=stage, summary=result.summary, details=result.details)
            if not self._is_resolved_blocker(child):
                ticket.blocked_by = sorted({*ticket.blocked_by, child.id})
            return
        if (
            workflow.id == "discovery"
            and ticket.type != "correction"
            and stage.id == "technical_analysis"
            and result.outcome == "completed"
        ):
            implementation_required, _ = _technical_analysis_plan(result.details)
            ticket.implementation_required = implementation_required
            self._create_delivery_children(ticket, result.details)

    def _create_corrective_child(self, parent: Ticket, *, child_type: str, status: str, stage: Stage, summary: str, details: str) -> Ticket:
        title = f"{child_type.capitalize()} for {parent.id}: {parent.title}"
        description = (
            f"Автоматически создано из {parent.id} на стадии {stage.title}.\n\n"
            f"Summary:\n{summary or '(пусто)'}\n\n"
            f"Details:\n{details or '(пусто)'}"
        )
        for existing in self.store.children_of(parent.id, process=parent.process):
            if existing.type == child_type and existing.description == description:
                if child_type == "rework":
                    self._inherit_rework_session(parent, existing)
                return existing
        correction_status = next(
            (candidate.id for candidate in self.workflows[parent.process].stages if candidate.kind == "queue" and candidate.pull_to == stage.id),
            stage.id,
        )
        child = self.store.create(
            parent.process,
            child_type,
            title,
            description=description,
            priority=parent.priority,
            parent=parent.id,
            status=correction_status if child_type == "correction" else status,
            wip_exempt=True,
            correction_stage=stage.id if child_type == "correction" else None,
            rework_stage=stage.id if child_type == "rework" else None,
        )
        if child_type == "rework":
            self._inherit_rework_session(parent, child)
        return child

    def _inherit_rework_session(self, parent: Ticket, rework: Ticket) -> None:
        if parent.process != "delivery" or rework.type != "rework":
            return
        for session in self.session_store.list():
            if session.status != "active":
                continue
            if parent.id not in self.session_store.effective_ticket_ids(session):
                continue
            try:
                self.session_store.inherit_ticket(session, rework.id, source_ticket=parent.id)
            except (KeyError, TypeError, ValueError) as exc:
                log.error("не удалось унаследовать сессию для %s от %s: %s", rework.id, parent.id, exc)
            return

    def _create_delivery_children(self, parent: Ticket, details: str) -> list[Ticket]:
        debt_tickets: list[Ticket] = []
        exact_debt_ticket_ids: set[str] = set()
        debt_service = TicketWriteService(self.store.project)
        for candidate in parse_technical_debt(details):
            key, basis = technical_debt_basis(candidate, self.store.project)
            result = debt_service.create_technical_debt_ticket(
                problem=candidate["problem"].strip(),
                evidence=candidate["evidence"],
                impact=candidate["impact"],
                suggested_scope=candidate["suggested_scope"],
                source_ticket=candidate["source_ticket"],
                source_stage=candidate["source_stage"],
                source_run=candidate["source_run"],
                dedup_key=key,
                dedup_basis=basis,
                priority=candidate["priority"],
                origin=f"technical_analysis:{candidate['source_run']}",
                actor="orchestrator",
            )
            if result.ticket is not None:
                debt_tickets.append(result.ticket)
                if result.status == "exact":
                    exact_debt_ticket_ids.add(result.ticket.id)
        spec = _extract_structured_payload(details).get("delivery_tickets", [])
        existing_children = {
            (child.type, child.title.strip()): child
            for child in self.store.children_of(parent.id, process="delivery")
            if child.type in {"story", "task", "bug"}
        }
        active_keys: set[tuple[str, str]] = set()
        synced: list[Ticket] = debt_tickets
        for item in spec:
            if not isinstance(item, dict):
                continue
            ticket_type = str(item.get("type", "")).lower()
            if ticket_type not in {"story", "task", "bug"}:
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            description = str(item.get("description", "")).strip()
            priority = int(item.get("priority", parent.priority))
            mandatory = bool(item.get("mandatory", True))
            key = (ticket_type, title)
            active_keys.add(key)
            existing = existing_children.get(key)
            if existing:
                if existing.id in exact_debt_ticket_ids:
                    synced.append(existing)
                    continue
                changed = False
                if existing.description != description or existing.priority != priority or existing.mandatory != mandatory:
                    existing.description = description
                    existing.priority = priority
                    existing.mandatory = mandatory
                    changed = True
                if existing.status == "selected_for_session" and not self._is_open_delivery_session_member(existing.id):
                    # Старые результаты technical_analysis могли ошибочно выбрать ребёнка
                    # без явного включения в draft/active Delivery-сессию.
                    existing.status = "todo"
                    changed = True
                if changed:
                    self.store.save(existing)
                synced.append(existing)
                continue
            child = self.store.create(
                "delivery",
                ticket_type,
                title,
                description=description,
                priority=priority,
                parent=parent.id,
                # Новая декомпозиция ждёт явного отбора человеком в Delivery-сессию.
                status="todo",
                mandatory=mandatory,
            )
            existing_children[key] = child
            synced.append(child)
        for key, child in existing_children.items():
            if key in active_keys or child.id in exact_debt_ticket_ids:
                continue
            self._deactivate_delivery_child(child)
        return synced

    def _is_open_delivery_session_member(self, ticket_id: str) -> bool:
        return any(
            session.status in {"draft", "active"}
            and ticket_id in self.session_store.effective_ticket_ids(session)
            for session in self.session_store.list()
        )

    def _deactivate_delivery_child(self, child: Ticket) -> None:
        # A stale decomposition entry may still contain real implementation
        # work. Never mark it done before its tree has reached the integration
        # target; otherwise the release reconciler will no longer see it.
        if self.tree_manager.enabled():
            record = self.tree_manager.trees.get(child.id)
            if record and record.integration_status != "merged":
                self.tree_manager.release(child)
        child.parent = None
        child.blocked_by = []
        child.active_run = None
        child.status = "done"
        self.store.save(child)

    def _release_parent_if_resolved(self, ticket: Ticket) -> None:
        if ticket.type not in {"rework", "correction"} or not ticket.parent or not self._is_resolved_blocker(ticket):
            return
        parent = self.store.get(ticket.parent)
        if ticket.id in parent.blocked_by:
            parent.blocked_by = [blocked for blocked in parent.blocked_by if blocked != ticket.id]
            self.store.save(parent)

    def _reconcile_tickets(self) -> None:
        self._reconcile_rework_sessions()
        self._reconcile_blockers()
        self._reconcile_discovery_implementation()
        self._reconcile_releases()

    def _reconcile_rework_sessions(self) -> None:
        """Repair session membership missed by an older or interrupted process."""
        for session in self.session_store.list():
            if session.status != "active":
                continue
            members = self.session_store.effective_ticket_ids(session)
            for parent_id in list(members):
                try:
                    parent = self.store.get(parent_id)
                except KeyError:
                    continue
                if parent.process != "delivery":
                    continue
                for rework in self.store.children_of(parent.id, process="delivery"):
                    if rework.type != "rework" or self.store.is_done(rework):
                        continue
                    if rework.id not in self.session_store.effective_ticket_ids(session):
                        try:
                            self.session_store.inherit_ticket(session, rework.id, source_ticket=parent.id)
                        except (KeyError, TypeError, ValueError) as exc:
                            log.error("не удалось восстановить membership %s для %s: %s", rework.id, session.id, exc)

    def _reconcile_releases(self) -> None:
        if not self.tree_manager.enabled():
            return
        for ticket in self.store.list("delivery"):
            if ticket.status != "ready_for_release" or ticket.active_run or ticket.blocked_by:
                continue
            record = self.tree_manager.trees.get(ticket.id)
            if record and record.integration_status == "conflict":
                continue
            try:
                self.tree_manager.release(ticket)
            except GitTreeError as exc:
                ticket.last_outcome = "integration_conflict"
                ticket.last_summary = str(exc)
                self.store.save(ticket)
                log.error("конфликт интеграции для %s (%s): %s", ticket.id, ticket.type, exc)
                continue
            ticket.status = "done"
            ticket.last_outcome = "completed"
            ticket.last_summary = "Дерево тикета интегрировано в целевую ветку"
            self.store.record_run_event(
                ticket,
                run_id=f"release-{ticket.id}",
                stage_id="release",
                event="completed",
                outcome="completed",
                summary=ticket.last_summary,
            )
            self.store.save(ticket)
            self._release_parent_if_resolved(ticket)

    def _reconcile_blockers(self) -> None:
        for ticket in self.store.list():
            if not ticket.blocked_by:
                continue
            remaining = []
            changed = False
            for blocked_id in ticket.blocked_by:
                try:
                    blocked = self.store.get(blocked_id)
                except KeyError:
                    changed = True
                    continue
                if self._is_resolved_blocker(blocked):
                    changed = True
                    continue
                remaining.append(blocked_id)
            if changed:
                ticket.blocked_by = remaining
                self.store.save(ticket)

    def _reconcile_discovery_implementation(self) -> None:
        workflow = self.workflows["discovery"]
        next_status = workflow.by_id["implementation"].next
        for ticket in self.store.list("discovery"):
            if ticket.status != "implementation" or ticket.active_run or ticket.blocked_by:
                continue
            linked = self.store.children_of(ticket.id, process="delivery")
            mandatory = [child for child in linked if child.mandatory]
            if ticket.implementation_required is not False and mandatory and any(not self.store.is_done(child) for child in mandatory):
                continue
            if next_status and ticket.status != next_status:
                ticket.status = next_status
                self.store.save(ticket)

    def _reap_finished(self) -> None:
        for ticket_id, task in list(self.running.items()):
            if task.done():
                try:
                    task.result()
                except Exception:
                    pass
                del self.running[ticket_id]

    def _is_resolved_blocker(self, ticket: Ticket) -> bool:
        return self.store.is_done(ticket)

    def _prompt_metadata(self, stage: Stage) -> dict[str, str]:
        if not stage.prompt:
            return {}
        return self.runner.prepare_execution_contract(stage, "metadata-only").history_metadata()

    def _run_traceability_metadata(
        self,
        ticket: Ticket,
        run_id: str | None,
        stage: Stage,
        *,
        fallback: dict[str, str] | None = None,
        suppress_prompt_errors: bool = False,
    ) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if run_id:
            for entry in ticket.run_history:
                if entry.get("run_id") != run_id or entry.get("event") != "started":
                    continue
                metadata.update({key: value for key in _TRACEABILITY_KEYS if (value := entry.get(key)) is not None})
                if entry.get("reservation") is not None:
                    metadata["reservation"] = entry["reservation"]
                break
        if len(metadata) < len(_TRACEABILITY_KEYS):
            if fallback:
                metadata.update({key: value for key, value in fallback.items() if key not in metadata})
            try:
                fallback = self._prompt_metadata(stage)
            except Exception:
                if not suppress_prompt_errors:
                    raise
                fallback = self.runner.execution_profile(stage)
            metadata.update({key: value for key, value in fallback.items() if key not in metadata})
        if len(metadata) < len(_TRACEABILITY_KEYS):
            metadata.update({key: value for key, value in ticket_prompt_metadata(ticket).items() if key not in metadata})
        return metadata


_TRACEABILITY_KEYS = (
    "model",
    "reasoning_effort",
    "prompt_path",
    "prompt_version",
    "ticket_title",
    "ticket_priority",
    "ticket_parent",
    "ticket_description",
)


def _extract_structured_payload(details: str) -> dict[str, object]:
    if not details.strip():
        return {}
    candidates = [details]
    candidates.extend(match.group(1) for match in re.finditer(r"```(?:ya?ml|json)?\n(.*?)```", details, flags=re.DOTALL))
    for candidate in candidates:
        try:
            payload = yaml.safe_load(candidate)
        except yaml.YAMLError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _extract_context_payload(details: str) -> dict[str, object]:
    payload = _extract_structured_payload(details)
    context = payload.get("context")
    return context if isinstance(context, dict) else {}


def _merge_context(current: dict[str, object], update: dict[str, object]) -> dict[str, object]:
    merged = deepcopy(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_context(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _technical_analysis_plan(details: str) -> tuple[bool, list[dict[str, object]]]:
    payload = _extract_structured_payload(details)
    implementation_required = payload.get("implementation_required")
    if not isinstance(implementation_required, bool):
        raise ValueError("Укажите implementation_required: true или implementation_required: false.")

    delivery_tickets = payload.get("delivery_tickets")
    if not isinstance(delivery_tickets, list):
        raise ValueError("Укажите delivery_tickets как YAML-список, даже если он пуст.")
    if any(not isinstance(item, dict) for item in delivery_tickets):
        raise ValueError("Каждый элемент delivery_tickets должен быть YAML-объектом.")

    typed_tickets = [dict(item) for item in delivery_tickets]
    for item in typed_tickets:
        if str(item.get("type", "")).lower() not in {"story", "task", "bug"}:
            raise ValueError("Каждый Delivery-тикет должен иметь type: story, task или bug.")
        if not str(item.get("title", "")).strip():
            raise ValueError("Каждый Delivery-тикет должен иметь непустой title.")
        if "mandatory" in item and not isinstance(item["mandatory"], bool):
            raise ValueError("Поле mandatory каждого Delivery-тикета должно быть true или false.")
        if "priority" in item and (not isinstance(item["priority"], int) or isinstance(item["priority"], bool)):
            raise ValueError("Поле priority каждого Delivery-тикета должно быть целым числом.")
    if not implementation_required and typed_tickets:
        raise ValueError("При implementation_required: false список delivery_tickets должен быть пустым.")
    if implementation_required:
        mandatory = [item for item in typed_tickets if item.get("mandatory", True) is True]
        if not mandatory:
            raise ValueError("При implementation_required: true нужен хотя бы один тикет с mandatory: true.")
    return implementation_required, typed_tickets
