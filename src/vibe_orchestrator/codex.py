from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable

import yaml

from . import __version__
from .config import PromptSpec, Stage, load_prompt_spec, package_root
from .tickets import Ticket, TicketStore
from .token_usage import parse_codex_usage, unknown_token_usage


@dataclass(frozen=True)
class AgentResult:
    outcome: str
    summary: str
    details: str = ""
    token_usage: dict[str, object] | None = None


@dataclass(frozen=True)
class ExecutionContract:
    run_id: str
    prompt_path: str
    prompt_body: str
    prompt_contract: str
    prompt_version: str
    model: str
    reasoning_effort: str
    reservation_metadata: dict[str, object] | None = None

    def history_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "prompt_path": self.prompt_path,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }
        if self.reservation_metadata is not None:
            metadata["reservation"] = self.reservation_metadata
        return metadata


def ticket_prompt_metadata(ticket: Ticket) -> dict[str, str]:
    return {
        "ticket_title": ticket.title,
        "ticket_priority": str(ticket.priority),
        "ticket_parent": ticket.parent or "none",
        "ticket_description": ticket.description or "(пусто)",
    }


class CodexRunner:
    def __init__(
        self,
        store: TicketStore,
        codex_binary: str = "codex",
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        self.store = store
        self.codex_binary = codex_binary
        self.default_model = model or os.environ.get("VIBE_CODEX_MODEL", "gpt-5.6-luna")
        self.default_reasoning_effort = reasoning_effort or os.environ.get("VIBE_CODEX_REASONING_EFFORT", "medium")

    def available(self) -> bool:
        return shutil.which(self.codex_binary) is not None

    def execution_profile(self, stage: Stage) -> dict[str, str]:
        return {
            "model": stage.model or self.default_model,
            "reasoning_effort": stage.reasoning_effort or self.default_reasoning_effort,
        }

    def prompt_spec(self, stage: Stage) -> PromptSpec:
        if not stage.prompt:
            raise ValueError(f"Stage {stage.id} has no prompt")
        return load_prompt_spec(stage.prompt)

    def prepare_execution_contract(self, stage: Stage, run_id: str | None = None) -> ExecutionContract:
        resolved_run_id = run_id or uuid.uuid4().hex
        prompt_spec = self.prompt_spec(stage)
        profile = self.execution_profile(stage)
        prompt_contract = self._prompt_contract(stage, prompt_spec, profile)
        return ExecutionContract(
            run_id=resolved_run_id,
            prompt_path=prompt_spec.path,
            prompt_body=prompt_spec.body,
            prompt_contract=prompt_contract,
            prompt_version=f"sha256:{sha256(prompt_contract.encode('utf-8')).hexdigest()}",
            model=profile["model"],
            reasoning_effort=profile["reasoning_effort"],
        )

    async def run(self, ticket: Ticket, stage: Stage, run_id: str | None = None, *, contract: ExecutionContract | None = None, workspace: Path | None = None, on_process_started: Callable[[str], None] | None = None) -> AgentResult:
        contract = contract or self.prepare_execution_contract(stage, run_id or ticket.active_run)
        run_id = contract.run_id
        run_dir = self.store.run_path(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        output_path = run_dir / "result.json"
        events_path = run_dir / "events.jsonl"
        manifest_path = run_dir / "run.json"
        prompt_path = run_dir / "prompt.txt"
        prompt_contract_path = run_dir / "prompt.contract.txt"
        workspace = workspace or self.store.project
        prompt = self._build_prompt(ticket, stage, contract, workspace=workspace)
        prompt_path.write_text(prompt, encoding="utf-8")
        prompt_contract_path.write_text(contract.prompt_contract, encoding="utf-8")
        manifest = {
            "run_id": run_id,
            "ticket_id": ticket.id,
            "process": ticket.process,
            "ticket_type": ticket.type,
            "stage": stage.id,
            "artifacts_path": f".vibe/runs/{run_id}",
            "prompt_path": contract.prompt_path,
            "prompt_version": contract.prompt_version,
            "prompt_artifact_path": f".vibe/runs/{run_id}/prompt.txt",
            "prompt_contract_artifact_path": f".vibe/runs/{run_id}/prompt.contract.txt",
            "prompt_contract": contract.prompt_contract,
            "prompt": prompt,
            "version": __version__,
            "model": contract.model,
            "reasoning_effort": contract.reasoning_effort,
            "reservation": contract.reservation_metadata,
            "ticket_snapshot": ticket_prompt_metadata(ticket),
            "workspace_path": str(workspace),
            "token_usage": unknown_token_usage(),
        }
        cmd = self._build_exec_args(output_path, contract=contract)
        manifest["command"] = cmd
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        process = await asyncio.create_subprocess_exec(*cmd, cwd=workspace, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        if on_process_started:
            on_process_started(run_id)
        stdout, _ = await process.communicate(prompt.encode("utf-8"))
        events_path.write_bytes(stdout or b"")
        token_usage = parse_codex_usage(
            stdout or b"",
            expected_run_id=contract.run_id,
            model=contract.model,
            reasoning_effort=contract.reasoning_effort,
        )
        manifest["token_usage"] = token_usage
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if process.returncode != 0:
            raise RuntimeError(f"Codex exited with {process.returncode}; see {events_path}")
        data = json.loads(output_path.read_text(encoding="utf-8"))
        data["token_usage"] = token_usage
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return AgentResult(outcome=data["outcome"], summary=data["summary"], details=data.get("details", ""), token_usage=token_usage)

    def _build_prompt(
        self,
        ticket: Ticket,
        stage: Stage,
        contract: ExecutionContract,
        workspace: Path | None = None,
    ) -> str:
        prompt = self._render_prompt(
            stage=stage,
            prompt_body=contract.prompt_body,
            repository_root=str(workspace or self.store.project),
            run_id=contract.run_id,
            ticket_id=ticket.id,
            process=ticket.process,
            ticket_type=ticket.type,
            title=ticket.title,
            priority=str(ticket.priority),
            parent=ticket.parent or "none",
            prompt_path=contract.prompt_path,
            prompt_version=contract.prompt_version,
            model=contract.model,
            reasoning_effort=contract.reasoning_effort,
            description=ticket.description or "(пусто)",
        )
        correction_context = self._correction_context(ticket)
        if correction_context:
            prompt += f"\n\n## Контекст завершённых Correction\n{correction_context}\n"
        if ticket.context:
            context_yaml = yaml.safe_dump(ticket.context, sort_keys=False, allow_unicode=True).rstrip()
            prompt += f"\n\n## Актуальный контекст тикета (ревизия {ticket.context_revision})\n```yaml\n{context_yaml}\n```\n"
        return prompt

    def _correction_context(self, ticket: Ticket) -> str:
        corrections = [
            child
            for child in self.store.children_of(ticket.id, process=ticket.process)
            if child.type == "correction" and self.store.is_done(child)
        ]
        if not corrections:
            return ""
        return "\n\n".join(
            f"Correction {child.id}:\nОписание: {child.description or '(пусто)'}\nИтог: {child.last_summary or '(пусто)'}"
            for child in corrections
        )

    def _prompt_contract(self, stage: Stage, prompt_spec: PromptSpec, profile: dict[str, str]) -> str:
        return self._render_prompt(
            stage=stage,
            prompt_body=prompt_spec.body,
            repository_root="{repository_root}",
            run_id="{run_id}",
            ticket_id="{ticket_id}",
            process="{process}",
            ticket_type="{ticket_type}",
            title="{title}",
            priority="{priority}",
            parent="{parent}",
            prompt_path=prompt_spec.path,
            prompt_version="{prompt_version}",
            model=profile["model"],
            reasoning_effort=profile["reasoning_effort"],
            description="{description}",
        )

    def _render_prompt(
        self,
        *,
        stage: Stage,
        prompt_body: str,
        repository_root: str,
        run_id: str,
        ticket_id: str,
        process: str,
        ticket_type: str,
        title: str,
        priority: str,
        parent: str,
        prompt_path: str,
        prompt_version: str,
        model: str,
        reasoning_effort: str,
        description: str,
    ) -> str:
        outcomes = ", ".join((stage.outcomes or {}).keys()) or "completed"
        return f"""{prompt_body}

## Контракт выполнения
Вы работаете ровно с одним тикетом и ровно на одной стадии workflow.
Repository root: {repository_root}
Run ID: {run_id}
Ticket ID: {ticket_id}
Process: {process}
Ticket type: {ticket_type}
Stage: {stage.id}
Title: {title}
Priority: {priority}
Parent: {parent}
Prompt path: {prompt_path}
Prompt version: {prompt_version}
Model: {model}
Reasoning effort: {reasoning_effort}
Описание:
{description}

Допустимые outcomes: {outcomes}.

Важно:
- Не редактируйте `.vibe/tickets/**` и не меняйте статус workflow самостоятельно.
- Вы можете изменять код и документацию проекта, если это требуется инструкциями стадии.
- Работайте от текущего состояния репозитория; перед действиями изучите релевантные файлы.
- Завершайте работу, возвращая только структурированный результат, требуемый переданной output schema.
- `outcome` должен быть одним из допустимых значений выше.
- Поместите компактный устойчивый handoff в `summary`; в `details` укажите полезные доказательства, пути, тесты или открытые вопросы.
"""

    def _build_exec_args(self, output_path: Path, *, contract: ExecutionContract) -> list[str]:
        schema_path = package_root() / "schemas" / "agent-result.schema.json"
        cmd = [
            self.codex_binary,
            "exec",
            "--sandbox",
            "workspace-write",
            "--json",
            "--model",
            contract.model,
            "-c",
            f"model_reasoning_effort={json.dumps(contract.reasoning_effort)}",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
        cmd.append("-")
        return cmd
