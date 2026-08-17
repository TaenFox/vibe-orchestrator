from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .codex import AgentResult, CodexRunner, ExecutionContract, ticket_prompt_metadata
from .config import Stage, Workflow, load_all_workflows
from .control import WorkerControl
from .scheduler import select_candidates
from .tickets import RETRY_BACKOFF_SECONDS, Ticket, TicketStore

log = logging.getLogger("vibe")


class Orchestrator:
    def __init__(self, project: Path, poll_interval: float = 2.0, max_agents: int | None = None):
        self.store = TicketStore(project)
        self.store.init()
        self.workflows = load_all_workflows()
        self.runner = CodexRunner(self.store)
        self.poll_interval = poll_interval
        self.worker_control = WorkerControl(project)
        initial_worker_limit = self.worker_control.get_limit() if max_agents is None else max_agents
        self.worker_control.set_limit(initial_worker_limit)
        self.max_agents = initial_worker_limit
        self._last_worker_limit = initial_worker_limit
        self.running: dict[str, asyncio.Task[None]] = {}

    async def run_forever(self) -> None:
        if not self.runner.available():
            raise RuntimeError("Codex CLI не найден в PATH. Установите Codex и выполните вход перед запуском оркестратора.")
        log.info("наблюдение за %s", self.store.project)
        while True:
            self._reap_finished()
            self._reconcile_tickets()
            await self._schedule_once()
            await asyncio.sleep(self.poll_interval)

    async def _schedule_once(self) -> None:
        worker_limit = self._read_worker_limit()
        slots = worker_limit - len(self.running)
        if slots <= 0:
            return
        global_candidates = []
        running_ids = set(self.running)
        for process, workflow in self.workflows.items():
            tickets = self.store.list(process)
            global_candidates.extend((workflow, c) for c in select_candidates(workflow, tickets, running_ids))
        global_candidates.sort(key=lambda item: (0 if item[1].ticket.wip_exempt else 1, item[1].ticket.priority, item[1].ticket.created_at))
        for workflow, candidate in global_candidates[:slots]:
            if len(self.running) >= self._read_worker_limit():
                break
            ticket = self.store.get(candidate.ticket.id)
            if ticket.active_run or ticket.blocked_by or ticket.status != candidate.source_status:
                continue
            stage = workflow.by_id[candidate.target_status]
            run_id = uuid.uuid4().hex
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
                    **metadata,
                    **ticket_prompt_metadata(ticket),
                )
                self.store.save(ticket)
                self._record_failure(ticket, run_id, candidate.target_status, exc, metadata)
                log.exception("сбой подготовки запуска для %s", ticket.id)
                continue
            ticket.status = candidate.target_status
            ticket.active_run = contract.run_id
            ticket.retry_after = None
            self.store.record_run_event(
                ticket,
                run_id=contract.run_id,
                stage_id=candidate.target_status,
                event="started",
                from_status=candidate.source_status,
                to_status=candidate.target_status,
                **contract.history_metadata(),
                **ticket_prompt_metadata(ticket),
            )
            self.store.save(ticket)
            task = asyncio.create_task(self._execute(workflow, ticket.id, candidate.target_status, contract), name=ticket.id)
            self.running[ticket.id] = task
            log.info("запущено %s -> %s", ticket.id, candidate.target_status)

    def _read_worker_limit(self) -> int:
        worker_limit = self.worker_control.get_limit(self._last_worker_limit)
        if worker_limit != self._last_worker_limit:
            log.info("лимит воркеров изменен: %s -> %s", self._last_worker_limit, worker_limit)
            self._last_worker_limit = worker_limit
        return worker_limit

    async def _execute(self, workflow: Workflow, ticket_id: str, stage_id: str, contract: ExecutionContract | str) -> None:
        ticket = self.store.get(ticket_id)
        stage = workflow.by_id[stage_id]
        metadata: dict[str, str] | None = None
        try:
            if isinstance(contract, str):
                metadata = self._run_traceability_metadata(ticket, contract, stage)
                contract = self.runner.prepare_execution_contract(stage, contract)
            else:
                metadata = contract.history_metadata()
            result = await self.runner.run(ticket, stage, contract.run_id, contract=contract)
            self._apply_result(workflow, ticket_id, stage, result, contract=contract)
            log.info("завершено %s: %s -> %s", ticket_id, result.outcome, self.store.get(ticket_id).status)
        except Exception as exc:
            ticket = self.store.get(ticket_id)
            if isinstance(contract, str):
                run_id = contract
                metadata = metadata or self._run_traceability_metadata(ticket, run_id, stage, suppress_prompt_errors=True)
            else:
                run_id = contract.run_id
                metadata = metadata or self._run_traceability_metadata(ticket, run_id, stage, fallback=contract.history_metadata())
            self._record_failure(ticket, run_id, stage_id, exc, metadata)
            log.exception("сбой воркера для %s", ticket_id)

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
            **metadata,
        )
        ticket.active_run = None
        ticket.last_outcome = "failed"
        ticket.last_summary = str(exc)
        self.store.save(ticket)

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
            except ValueError as exc:
                result = AgentResult(
                    outcome="needs_correction",
                    summary="Технический анализ вернул некорректный план реализации",
                    details=str(exc),
                )
        ticket = self.store.get(ticket_id)
        active_run = contract.run_id if contract else run_id or ticket.active_run
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
        ticket.status = (stage.outcomes or {})[result.outcome]
        if active_run:
            self.store.record_run_event(
                ticket,
                run_id=active_run,
                stage_id=stage.id,
                event="completed",
                outcome=result.outcome,
                summary=result.summary,
                to_status=ticket.status,
                **metadata,
            )
        ticket.active_run = None
        self._handle_follow_up(ticket, workflow, stage, result)
        self.store.save(ticket)
        self._release_parent_if_resolved(ticket)
        self._reconcile_tickets()

    def _handle_follow_up(self, ticket: Ticket, workflow: Workflow, stage: Stage, result: AgentResult) -> None:
        if workflow.id == "delivery" and result.outcome == "needs_rework":
            child = self._create_corrective_child(ticket, child_type="rework", status="selected_for_session", stage=stage, summary=result.summary, details=result.details)
            ticket.blocked_by = sorted({*ticket.blocked_by, child.id})
            return
        if workflow.id == "discovery" and result.outcome == "needs_correction":
            if stage.id == "technical_analysis":
                ticket.implementation_required = None
            child = self._create_corrective_child(ticket, child_type="correction", status="ready", stage=stage, summary=result.summary, details=result.details)
            ticket.blocked_by = sorted({*ticket.blocked_by, child.id})
            return
        if workflow.id == "discovery" and stage.id == "technical_analysis" and result.outcome == "completed":
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
        return self.store.create(
            parent.process,
            child_type,
            title,
            description=description,
            priority=parent.priority,
            parent=parent.id,
            status=status,
            wip_exempt=True,
        )

    def _create_delivery_children(self, parent: Ticket, details: str) -> list[Ticket]:
        spec = _extract_structured_payload(details).get("delivery_tickets", [])
        existing_children = {
            (child.type, child.title.strip()): child
            for child in self.store.children_of(parent.id, process="delivery")
            if child.type in {"story", "task", "bug"}
        }
        active_keys: set[tuple[str, str]] = set()
        synced: list[Ticket] = []
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
                if existing.description != description or existing.priority != priority or existing.mandatory != mandatory:
                    existing.description = description
                    existing.priority = priority
                    existing.mandatory = mandatory
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
                status="selected_for_session",
                mandatory=mandatory,
            )
            existing_children[key] = child
            synced.append(child)
        for key, child in existing_children.items():
            if key in active_keys:
                continue
            self._deactivate_delivery_child(child)
        return synced

    def _deactivate_delivery_child(self, child: Ticket) -> None:
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
        self._reconcile_blockers()
        self._reconcile_discovery_implementation()

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
        if ticket.process == "delivery" and ticket.type == "rework":
            workflow = self.workflows[ticket.process]
            return workflow.position(ticket.status) >= workflow.position("ready_for_release")
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
