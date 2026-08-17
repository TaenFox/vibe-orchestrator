from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml


class WorkerControl:
    def __init__(self, project: Path):
        self.path = project.resolve() / ".vibe" / "tmp" / "workers.yaml"

    def get_limit(self, default: int = 8) -> int:
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            limit = payload.get("max_workers") if isinstance(payload, dict) else None
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                return default
            return limit
        except (OSError, UnicodeError, yaml.YAMLError):
            return default

    def set_limit(self, limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("Количество воркеров должно быть целым неотрицательным числом")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump({"max_workers": limit}, sort_keys=False, allow_unicode=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class DeliverySessionControl:
    """Read the optional active Delivery session selected by process management."""

    def __init__(self, project: Path):
        self.path = project.resolve() / ".vibe" / "tmp" / "delivery-session.yaml"

    def get_participants(self) -> set[str] | None:
        """Return participant IDs, or ``None`` when no active session exists.

        Missing and malformed state is deliberately treated as legacy mode so
        old projects continue to be schedulable without a migration.
        """
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        if not isinstance(payload, dict) or payload.get("active") is not True:
            return None
        participants = payload.get("participants")
        if not isinstance(participants, list) or any(not isinstance(item, str) or not item for item in participants):
            return None
        return set(participants)
