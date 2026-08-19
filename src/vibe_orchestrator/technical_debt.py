from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import load_workflow
from .sessions import SessionStore
from .tickets import TicketStore

CONTRACT_VERSION = "tech_debt_candidates.v1"
ERROR_VERSION = "orchestrator.errors.v1"
ERROR_CODES = {
    "TECH_DEBT_INVALID",
    "TECH_DEBT_SOURCE_NOT_FOUND",
    "TECH_DEBT_SOURCE_MISMATCH",
    "TECH_DEBT_PREFLIGHT_UNAVAILABLE",
    "TECH_DEBT_MUTATION_BLOCKED",
}
_FIELDS = {"problem", "evidence", "impact", "suggested_scope", "source_ticket", "source_stage", "source_run", "type", "urgency", "priority"}
ObservationVerifier = Callable[[str, str, str], bool]


def normalize_technical_debt_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("technical-debt basis values must be strings")
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip()).casefold()


def technical_debt_basis(candidate: dict[str, Any], project: Path) -> tuple[str, dict[str, Any]]:
    """Build immutable canonical basis and key after candidate preflight."""
    evidence = candidate.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("technical-debt evidence must be an object")
    root = project.resolve()
    evidence_path = (root / str(evidence.get("path", ""))).resolve()
    try:
        evidence_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("technical-debt evidence path escapes project root") from exc
    relative = evidence_path.relative_to(root).as_posix()
    if relative.startswith((".vibe/archive/", ".vibe/runs/")) or evidence_path.is_dir():
        raise ValueError("technical-debt evidence path is not allowed")
    basis = {
        "problem": normalize_technical_debt_text(candidate["problem"]),
        "area": normalize_technical_debt_text(candidate["suggested_scope"]),
        "evidence": {
            "path": normalize_technical_debt_text(relative),
            "identifier": normalize_technical_debt_text(evidence["identifier"]),
            "observation": normalize_technical_debt_text(evidence["observation"]),
        },
    }
    encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"tech_debt.v1:{hashlib.sha256(encoded).hexdigest()}", basis


@dataclass(frozen=True)
class TechnicalDebtError(ValueError):
    code: str
    path: str
    message: str
    details: dict[str, Any] | None = None

    @property
    def envelope(self) -> dict[str, Any]:
        return {"contract_version": ERROR_VERSION, "code": self.code, "path": self.path, "message": self.message, "details": self.details or {}}

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


def _error(path: str, message: str, *, code: str = "TECH_DEBT_INVALID", expected: str | None = None, actual: Any = None) -> TechnicalDebtError:
    details = {}
    if expected is not None:
        details["expected"] = expected
    if actual is not None:
        details["actual"] = actual
    return TechnicalDebtError(code, path, message, details)


def _payload(details: str) -> dict[str, Any]:
    candidates = [details, *[m.group(1) for m in re.finditer(r"```(?:ya?ml|json)?\n(.*?)```", details, re.DOTALL)]]
    for candidate in candidates:
        try:
            value = yaml.safe_load(candidate)
        except yaml.YAMLError:
            if "tech_debt_candidates:" in candidate:
                raise _error("tech_debt_candidates", "Некорректный YAML-контракт.")
            continue
        if isinstance(value, dict):
            return value
    return {}


def parse_technical_debt(details: str) -> list[dict[str, Any]]:
    """Parse and validate the optional root-level contract; absence is empty."""
    payload = _payload(details or "")
    if "tech_debt_candidates" not in payload:
        return []
    root = payload["tech_debt_candidates"]
    if not isinstance(root, dict):
        raise _error("tech_debt_candidates", "Ожидался YAML-объект контракта.")
    allowed = {"version", "candidates"}
    unknown = sorted(set(root) - allowed)
    if unknown:
        raise _error(f"tech_debt_candidates.{unknown[0]}", "Неизвестное поле контракта.", expected="version или candidates", actual=unknown[0])
    if root.get("version") != CONTRACT_VERSION:
        raise _error("tech_debt_candidates.version", "Неподдерживаемая версия контракта.", expected=CONTRACT_VERSION, actual=root.get("version"))
    candidates = root.get("candidates")
    if not isinstance(candidates, list):
        raise _error("tech_debt_candidates.candidates", "Ожидался YAML-список кандидатов.")
    normalized = []
    for index, raw in enumerate(candidates):
        base = f"tech_debt_candidates.candidates[{index}]"
        if not isinstance(raw, dict):
            raise _error(base, "Кандидат должен быть YAML-объектом.")
        unknown = sorted(set(raw) - _FIELDS)
        if unknown:
            raise _error(f"{base}.{unknown[0]}", "Неизвестное поле кандидата.")
        item = dict(raw)
        for field in ("problem", "impact", "suggested_scope", "source_ticket", "source_stage", "source_run"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _error(f"{base}.{field}", "Обязательное непустое строковое поле.")
            item[field] = value.strip()
        evidence = item.get("evidence")
        if not isinstance(evidence, dict):
            raise _error(f"{base}.evidence", "Ожидался YAML-объект evidence.")
        unknown = sorted(set(evidence) - {"path", "identifier", "observation"})
        if unknown:
            raise _error(f"{base}.evidence.{unknown[0]}", "Неизвестное поле evidence.")
        item["evidence"] = {}
        for field in ("path", "identifier", "observation"):
            value = evidence.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _error(f"{base}.evidence.{field}", "Evidence должно содержать конкретное непустое наблюдение.")
            item["evidence"][field] = value.strip()
        if item.get("type") != "task":
            raise _error(f"{base}.type", "Допустим только type: task.", expected="task", actual=item.get("type"))
        if item.get("urgency") not in {"low", "medium", "high"}:
            raise _error(f"{base}.urgency", "Допустимы urgency: low, medium или high.")
        priority = item.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            raise _error(f"{base}.priority", "Priority должен быть целым неотрицательным числом.")
        normalized.append(item)
    return normalized


def preflight_technical_debt(
    candidates: list[dict[str, Any]],
    *,
    project: Path,
    ticket_store: TicketStore | None = None,
    session_store: SessionStore | None = None,
    reader: Callable[[Path], str] | None = None,
    observation_verifier: ObservationVerifier | None = None,
) -> list[dict[str, Any]]:
    """Read-only verification of all sources and evidence before mutation."""
    store = ticket_store or TicketStore(project)
    sessions = session_store or SessionStore(project, store)
    root = project.resolve()
    read = reader or (lambda path: path.read_text(encoding="utf-8"))
    try:
        if sessions.database_enabled:
            # Runtime preflight reads the same SQLite source of truth as the
            # scheduler; it must not recreate or inspect the removed YAML tree.
            sessions.list()
        else:
            # Explicit legacy mode is used only by the one-time migration path.
            for session_path in sorted(sessions.sessions_root.glob("*.yaml")):
                sessions.load_path(session_path)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise _error("tech_debt_candidates", "Delivery-сессии недоступны для read-only проверки.", code="TECH_DEBT_PREFLIGHT_UNAVAILABLE") from exc
    for index, candidate in enumerate(candidates):
        base = f"tech_debt_candidates.candidates[{index}]"
        try:
            source = store.get(candidate["source_ticket"])
        except (KeyError, OSError, ValueError) as exc:
            raise _error(f"{base}.source_ticket", "Исходный тикет не найден.", code="TECH_DEBT_SOURCE_NOT_FOUND") from exc
        try:
            workflow = load_workflow(source.process)
            if candidate["source_stage"] not in workflow.by_id:
                raise KeyError(candidate["source_stage"])
        except (KeyError, OSError, ValueError) as exc:
            raise _error(f"{base}.source_stage", "Стадия отсутствует в workflow исходного тикета.", code="TECH_DEBT_SOURCE_NOT_FOUND") from exc
        matching_history = [entry for entry in source.run_history if entry.get("run_id") == candidate["source_run"]]
        manifest = store.run_path(candidate["source_run"]) / "run.json"
        manifest_present = manifest.exists()
        if manifest_present:
            try:
                manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise _error(f"{base}.source_run", "Артефакт запуска недоступен.", code="TECH_DEBT_PREFLIGHT_UNAVAILABLE") from exc
            if not isinstance(manifest_data, dict):
                raise _error(f"{base}.source_run", "Артефакт запуска имеет недопустимый формат.", code="TECH_DEBT_PREFLIGHT_UNAVAILABLE")
            metadata = manifest_data
        else:
            if not matching_history:
                raise _error(f"{base}.source_run", "Запуск не найден.", code="TECH_DEBT_SOURCE_NOT_FOUND")
            metadata = matching_history[-1]
        if metadata.get("run_id") != candidate["source_run"]:
            raise _error(
                f"{base}.source_run",
                "Метаданные запуска не согласованы с source_run кандидата.",
                code="TECH_DEBT_SOURCE_MISMATCH",
                expected=candidate["source_run"],
                actual=metadata.get("run_id"),
            )
        if any(
            not isinstance(metadata.get(field), str)
            or not metadata[field].strip()
            or metadata[field] != expected
            for field, expected in (("ticket_id", source.id), ("stage", candidate["source_stage"]))
        ):
            raise _error(f"{base}.source_run", "Метаданные запуска не согласованы с источником.", code="TECH_DEBT_SOURCE_MISMATCH")
        evidence_path = (root / candidate["evidence"]["path"]).resolve()
        try:
            evidence_path.relative_to(root)
        except ValueError as exc:
            raise _error(f"{base}.evidence.path", "Путь evidence выходит за project root.", code="TECH_DEBT_SOURCE_MISMATCH") from exc
        relative = evidence_path.relative_to(root).as_posix()
        if relative.startswith(".vibe/archive/") or relative.startswith(".vibe/runs/") or evidence_path.is_dir():
            raise _error(f"{base}.evidence.path", "Путь запрещен для evidence.", code="TECH_DEBT_SOURCE_MISMATCH")
        try:
            content = read(evidence_path)
        except (OSError, UnicodeError) as exc:
            raise _error(f"{base}.evidence", "Evidence-файл недоступен для read-only проверки.", code="TECH_DEBT_PREFLIGHT_UNAVAILABLE") from exc
        identifier = candidate["evidence"]["identifier"]
        if identifier not in content:
            raise _error(f"{base}.evidence.identifier", "Identifier не найден в evidence-файле.", code="TECH_DEBT_SOURCE_MISMATCH")
        if observation_verifier is None:
            raise _error(
                f"{base}.evidence.observation",
                "Capability проверки observation недоступна.",
                code="TECH_DEBT_PREFLIGHT_UNAVAILABLE",
            )
        try:
            verified = observation_verifier(content, identifier, candidate["evidence"]["observation"])
        except Exception as exc:
            raise _error(
                f"{base}.evidence.observation",
                "Capability проверки observation недоступна.",
                code="TECH_DEBT_PREFLIGHT_UNAVAILABLE",
            ) from exc
        if not isinstance(verified, bool):
            raise _error(
                f"{base}.evidence.observation",
                "Capability проверки observation вернула недопустимый результат.",
                code="TECH_DEBT_PREFLIGHT_UNAVAILABLE",
            )
        if not verified:
            raise _error(
                f"{base}.evidence.observation",
                "Observation не подтверждено для evidence-файла.",
                code="TECH_DEBT_SOURCE_MISMATCH",
            )
    return candidates
