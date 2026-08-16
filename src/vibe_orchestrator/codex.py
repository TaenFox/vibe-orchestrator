from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass

from .config import Stage, load_prompt, package_root
from .tickets import Ticket, TicketStore


@dataclass(frozen=True)
class AgentResult:
    outcome: str
    summary: str
    details: str = ""


class CodexRunner:
    def __init__(self, store: TicketStore, codex_binary: str = "codex"):
        self.store = store
        self.codex_binary = codex_binary

    def available(self) -> bool:
        return shutil.which(self.codex_binary) is not None

    async def run(self, ticket: Ticket, stage: Stage) -> AgentResult:
        if not stage.prompt:
            raise ValueError(f"Stage {stage.id} has no prompt")
        run_id = uuid.uuid4().hex
        run_dir = self.store.runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        output_path = run_dir / "result.json"
        events_path = run_dir / "events.jsonl"
        schema_path = package_root() / "schemas" / "agent-result.schema.json"
        prompt = self._build_prompt(ticket, stage)
        cmd = [self.codex_binary, "exec", "--sandbox", "workspace-write", "--json", "--output-schema", str(schema_path), "-o", str(output_path), "-"]
        process = await asyncio.create_subprocess_exec(*cmd, cwd=self.store.project, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await process.communicate(prompt.encode("utf-8"))
        events_path.write_bytes(stdout or b"")
        if process.returncode != 0:
            raise RuntimeError(f"Codex exited with {process.returncode}; see {events_path}")
        data = json.loads(output_path.read_text(encoding="utf-8"))
        return AgentResult(outcome=data["outcome"], summary=data["summary"], details=data.get("details", ""))

    def _build_prompt(self, ticket: Ticket, stage: Stage) -> str:
        stage_prompt = load_prompt(stage.prompt)
        outcomes = ", ".join((stage.outcomes or {}).keys())
        return f"""{stage_prompt}

## Execution contract
You are working on exactly one ticket at exactly one workflow stage.
Repository root: {self.store.project}
Ticket ID: {ticket.id}
Process: {ticket.process}
Ticket type: {ticket.type}
Stage: {stage.id}
Title: {ticket.title}
Priority: {ticket.priority}
Parent: {ticket.parent or 'none'}
Description:
{ticket.description or '(empty)'}

Allowed outcomes: {outcomes or 'completed'}.

Important:
- Do not edit `.vibe/tickets/**` or change workflow status yourself.
- You may edit project code/docs when the stage instructions require it.
- Work from the current repository state; inspect relevant files before acting.
- Finish by returning only the structured result required by the supplied output schema.
- `outcome` must be one of the allowed outcomes above.
- Put a compact durable handoff in `summary`; use `details` for useful evidence, paths, tests, or open concerns.
"""
