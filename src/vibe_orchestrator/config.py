from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Stage:
    id: str
    title: str
    kind: str
    wip: int | None = None
    pull_to: str | None = None
    prompt: str | None = None
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
        raise KeyError(f"Unknown status {status!r} in workflow {self.id!r}")


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workflow_dir() -> Path:
    return package_root() / "workflows"


def prompt_dir() -> Path:
    return package_root() / "prompts"


def load_workflow(process: str) -> Workflow:
    path = workflow_dir() / f"{process}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Workflow not found: {path}")
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    stages = [
        Stage(
            id=item["id"],
            title=item.get("title", item["id"]),
            kind=item["kind"],
            wip=item.get("wip"),
            pull_to=item.get("pull_to"),
            prompt=item.get("prompt"),
            outcomes=item.get("outcomes"),
            next=item.get("next"),
        )
        for item in data["stages"]
    ]
    return Workflow(
        id=data["id"],
        title=data.get("title", data["id"]),
        initial_status=data.get("initial_status", stages[0].id),
        stages=stages,
    )


def load_all_workflows() -> dict[str, Workflow]:
    return {path.stem: load_workflow(path.stem) for path in workflow_dir().glob("*.yaml")}


def load_prompt(relative_path: str) -> str:
    path = prompt_dir() / relative_path
    return path.read_text(encoding="utf-8")
