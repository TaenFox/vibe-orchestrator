from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


class EnvironmentError(RuntimeError):
    """Project environment could not be prepared or verified."""


@dataclass(frozen=True)
class EnvironmentSpec:
    interpreter: str
    setup: str | None = None
    check: str | None = None

    @classmethod
    def from_project(cls, project: Path) -> "EnvironmentSpec | None":
        path = project / "pyproject.toml"
        if not path.exists():
            return None
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise EnvironmentError(f"Не удалось прочитать pyproject.toml: {exc}") from exc
        config = payload.get("tool", {}).get("vibe", {}).get("environment")
        if not isinstance(config, dict):
            return None
        interpreter = config.get("interpreter", ".venv/bin/python")
        setup = config.get("setup")
        check = config.get("check")
        if not isinstance(interpreter, str) or not interpreter.strip():
            raise EnvironmentError("tool.vibe.environment.interpreter должен быть непустой строкой")
        if setup is not None and not isinstance(setup, str):
            raise EnvironmentError("tool.vibe.environment.setup должен быть строкой")
        if check is not None and not isinstance(check, str):
            raise EnvironmentError("tool.vibe.environment.check должен быть строкой")
        return cls(interpreter.strip(), setup.strip() if setup else None, check.strip() if check else None)


class ProjectEnvironment:
    """Prepare one project environment and expose it to every worktree agent."""

    def __init__(self, project: Path):
        self.project = project.resolve()
        self.spec = EnvironmentSpec.from_project(self.project)
        self.interpreter: Path | None = None
        self._env: dict[str, str] | None = None

    def ensure(self) -> None:
        if self.spec is None:
            return
        # Keep the symlink path (not the real Homebrew/system Python target),
        # otherwise VIRTUAL_ENV would point outside the project environment.
        self.interpreter = (self.project / self.spec.interpreter).absolute()
        if not self.interpreter.exists():
            self.interpreter.parent.parent.mkdir(parents=True, exist_ok=True)
            self._run([sys.executable, "-m", "venv", str(self.interpreter.parent.parent)], check=True)
        env = self.environment()
        marker = self.project / ".vibe" / "tmp" / "environment.json"
        fingerprint = self._fingerprint()
        current = None
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            pass
        if not isinstance(current, dict) or current.get("fingerprint") != fingerprint:
            if self.spec.setup:
                self._run(self._command(self.spec.setup), env=env, check=True)
            if self.spec.check:
                self._run(self._command(self.spec.check), env=env, check=True)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"fingerprint": fingerprint}, sort_keys=True) + "\n", encoding="utf-8")
        self._env = env

    def environment(self) -> dict[str, str]:
        if self.interpreter is None:
            if self.spec is None:
                return dict(os.environ)
            self.interpreter = (self.project / self.spec.interpreter).absolute()
        virtual_env = self.interpreter.parent.parent
        env = dict(os.environ)
        env["VIRTUAL_ENV"] = str(virtual_env)
        env["PATH"] = f"{self.interpreter.parent}{os.pathsep}{env.get('PATH', '')}"
        return env

    def agent_environment(self) -> dict[str, str] | None:
        return dict(self._env) if self._env is not None else None

    def instructions(self) -> str | None:
        if self.spec is None or self.interpreter is None:
            return None
        check = self.spec.check or "(проверка не задана)"
        return (
            "Оркестратор подготовил общее окружение проекта для этого worktree. "
            "Используйте команды `python` и `pytest` из PATH; не ожидайте `.venv` "
            "внутри каталога worktree и не создавайте собственное окружение. "
            f"Команда проверки проекта: `{check}`."
        )

    def _fingerprint(self) -> str:
        source = (self.project / "pyproject.toml").read_bytes()
        payload = source + json.dumps(self.spec.__dict__, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _command(self, command: str) -> list[str]:
        try:
            return shlex.split(command)
        except ValueError as exc:
            raise EnvironmentError(f"Некорректная bootstrap-команда: {command}") from exc

    def _run(self, command: list[str], *, env: dict[str, str] | None = None, check: bool) -> None:
        try:
            subprocess.run(command, cwd=self.project, env=env, check=check, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            raise EnvironmentError(f"Не удалось выполнить подготовку окружения: {' '.join(command)}") from exc
