from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PromptSpec:
    path: str
    body: str
    version: str


@dataclass(frozen=True)
class Stage:
    id: str
    title: str
    kind: str
    wip: int | None = None
    pull_to: str | None = None
    prompt: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    outcomes: dict[str, str] | None = None
    next: str | None = None


@dataclass(frozen=True)
class Workflow:
    id: str
    title: str
    initial_status: str
    stages: list[Stage]

    @property
    def by_id(self) -> dict[str, Stage]:
        return {stage.id: stage for stage in self.stages}

    def position(self, status: str) -> int:
        for index, stage in enumerate(self.stages):
            if stage.id == status:
                return index
        raise KeyError(f"Неизвестный статус {status!r} в workflow {self.id!r}")


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workflow_dir() -> Path:
    return package_root() / "workflows"


def prompt_dir() -> Path:
    return package_root() / "prompts"


def load_workflow(process: str) -> Workflow:
    path = workflow_dir() / f"{process}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Workflow не найден: {path}")
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    stages = [_load_stage(item) for item in data["stages"]]
    return Workflow(
        id=data["id"],
        title=data.get("title", data["id"]),
        initial_status=data.get("initial_status", stages[0].id),
        stages=stages,
    )


def load_all_workflows() -> dict[str, Workflow]:
    return {path.stem: load_workflow(path.stem) for path in workflow_dir().glob("*.yaml")}


def load_prompt(relative_path: str) -> str:
    return load_prompt_spec(relative_path).body


def load_prompt_spec(relative_path: str) -> PromptSpec:
    path = prompt_dir() / relative_path
    body = path.read_text(encoding="utf-8")
    version = f"sha256:{sha256(body.encode('utf-8')).hexdigest()}"
    return PromptSpec(path=relative_path, body=body, version=version)


def _load_stage(item: dict[str, Any]) -> Stage:
    stage = Stage(
        id=item["id"],
        title=item.get("title", item["id"]),
        kind=item["kind"],
        wip=item.get("wip"),
        pull_to=item.get("pull_to"),
        prompt=item.get("prompt"),
        model=item.get("model"),
        reasoning_effort=item.get("reasoning_effort"),
        outcomes=item.get("outcomes"),
        next=item.get("next"),
    )
    if stage.kind != "agent":
        return stage
    missing = [field for field in ("prompt", "model", "reasoning_effort") if not getattr(stage, field)]
    if missing:
        raise ValueError(f"Agent stage {stage.id!r} must declare {', '.join(missing)}")
    return stage
