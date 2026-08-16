from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from .codex import CodexRunner
from .config import Workflow, load_all_workflows
from .scheduler import select_candidates
from .tickets import TicketStore

log = logging.getLogger("vibe")


class Orchestrator:
    def __init__(self, project: Path, poll_interval: float = 2.0, max_agents: int = 8):
        self.store = TicketStore(project)
        self.store.init()
        self.workflows = load_all_workflows()
        self.runner = CodexRunner(self.store)
        self.poll_interval = poll_interval
        self.max_agents = max_agents
        self.running: dict[str, asyncio.Task[None]] = {}

    async def run_forever(self) -> None:
        if not self.runner.available():
            raise RuntimeError("Codex CLI не найден в PATH. Установите Codex и выполните вход перед запуском оркестратора.")
        log.info("наблюдение за %s", self.store.project)
        while True:
            self._reap_finished()
            await self._schedule_once()
            await asyncio.sleep(self.poll_interval)

    async def _schedule_once(self) -> None:
        slots = self.max_agents - len(self.running)
        if slots <= 0:
            return
        global_candidates = []
        running_ids = set(self.running)
        for process, workflow in self.workflows.items():
            tickets = self.store.list(process)
            global_candidates.extend((workflow, c) for c in select_candidates(workflow, tickets, running_ids))
        global_candidates.sort(key=lambda item: (0 if item[1].ticket.wip_exempt else 1, item[1].ticket.priority, item[1].ticket.created_at))
        for workflow, candidate in global_candidates[:slots]:
            ticket = self.store.get(candidate.ticket.id)
            if ticket.active_run or ticket.blocked_by or ticket.status != candidate.source_status:
                continue
            run_id = uuid.uuid4().hex
            ticket.status = candidate.target_status
            ticket.active_run = run_id
            self.store.save(ticket)
            task = asyncio.create_task(self._execute(workflow, ticket.id, candidate.target_status), name=ticket.id)
            self.running[ticket.id] = task
            log.info("запущено %s -> %s", ticket.id, candidate.target_status)

    async def _execute(self, workflow: Workflow, ticket_id: str, stage_id: str) -> None:
        ticket = self.store.get(ticket_id)
        stage = workflow.by_id[stage_id]
        try:
            result = await self.runner.run(ticket, stage)
            if result.outcome not in (stage.outcomes or {}):
                raise ValueError(f"Outcome {result.outcome!r} is not allowed for {workflow.id}/{stage.id}")
            ticket = self.store.get(ticket_id)
            ticket.last_outcome = result.outcome
            ticket.last_summary = result.summary
            ticket.status = (stage.outcomes or {})[result.outcome]
            ticket.active_run = None
            self.store.save(ticket)
            log.info("завершено %s: %s -> %s", ticket.id, result.outcome, ticket.status)
        except Exception as exc:
            ticket = self.store.get(ticket_id)
            ticket.active_run = None
            ticket.last_outcome = "failed"
            ticket.last_summary = str(exc)
            self.store.save(ticket)
            log.exception("сбой воркера для %s", ticket_id)

    def _reap_finished(self) -> None:
        for ticket_id, task in list(self.running.items()):
            if task.done():
                try:
                    task.result()
                except Exception:
                    pass
                del self.running[ticket_id]
