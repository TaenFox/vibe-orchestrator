"""Deterministic, redacted workload generation for the performance audit."""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from vibe_orchestrator.budget_ledger import BudgetLedger
from vibe_orchestrator.sessions import DeliverySession, SessionStore
from vibe_orchestrator.tickets import Ticket, TicketStore

SCHEMA_VERSION = "performance-fixture.v2"
SIZES = {"small": 100, "medium": 1000, "large": 5000, "xlarge": 10000}
STATUSES = ("todo", "selected_for_session", "system_analysis", "development", "review", "blocked", "done")
SESSION_STATES = ("draft", "active", "completed", "cancelled")
BUDGET_STATES = ("active", "exhausted", "blocked_unknown", "over_budget")
RUNS_PER_TICKET = (0, 1, 10)
SESSION_COUNTS = (1, 10, 100)
LEDGER_RUN_COUNTS = (100, 1000, 10000)
REDACTION_POLICY = "synthetic identifiers and counts only; no non-empty titles, descriptions, prompts or raw payloads"


def _logical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if ".git" in path.parts or path.name in {"control.sqlite3", "ledger.sqlite3"}:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _logical_manifest(seed: int, size: str, storage_mode: str, statuses: dict[str, int],
                     ticket_ids: list[str], session_counts: dict[str, int], ledger_count: int,
                     budget_counts: dict[str, int]) -> dict[str, Any]:
    logical = {
        "schema_version": SCHEMA_VERSION, "seed": seed, "size": size, "storage_mode": storage_mode,
        "ticket_count": len(ticket_ids), "ticket_ids": ticket_ids, "ticket_status": statuses,
        "session_states": session_counts, "ledger_runs": ledger_count, "runs_per_ticket": list(RUNS_PER_TICKET),
        "budget_states": budget_counts,
    }
    return {"sha256": _logical_hash(logical), "logical": logical}


def generate_fixture(project: Path, *, seed: int = 35527, size: str = "small",
                     storage_mode: str = "sqlite", include_legacy: bool = True) -> dict[str, Any]:
    """Materialise a complete synthetic profile; no production text is persisted."""
    if size not in SIZES:
        raise ValueError(f"unknown size: {size}")
    if storage_mode not in {"sqlite", "yaml", "legacy"}:
        raise ValueError(f"unknown storage mode: {storage_mode}")
    storage_mode = "yaml" if storage_mode == "legacy" else storage_mode
    project = Path(project).resolve()
    rng = random.Random(seed)
    use_database = storage_mode == "sqlite"
    store = TicketStore(project, use_database=use_database)
    store.init()
    count = SIZES[size]
    order = list(range(count))
    rng.shuffle(order)
    ticket_ids: list[str] = []
    statuses = {status: 0 for status in STATUSES}
    for position, source_index in enumerate(order):
        status = STATUSES[rng.randrange(len(STATUSES))]
        ticket_id = f"FIX-{seed % 100000:05d}-{source_index:05d}"
        ticket_ids.append(ticket_id)
        statuses[status] += 1
        store.save(Ticket(
            id=ticket_id, process="delivery", type="story", title="", description="", status=status,
            priority=1 + rng.randrange(100),
            parent=ticket_ids[position - 1] if position and rng.random() < .06 else None,
            blocked_by=[ticket_ids[position - 2]] if position > 1 and rng.random() < .04 else [],
            run_history=[{"outcome": "completed", "attempt": n} for n in range(source_index % 11)],
            created_at="2024-01-01T00:00:00+00:00", updated_at="2024-01-01T00:00:00+00:00"))

    sessions = SessionStore(project, store, use_database=use_database)
    sessions.init()
    session_count = min(100, max(4, count // 10))
    session_counts = {state: 0 for state in SESSION_STATES}
    for index in range(session_count):
        state = SESSION_STATES[index % len(SESSION_STATES)]
        session_counts[state] += 1
        session = DeliverySession(
            id=f"SESSION-{seed % 100000:05d}-{index:04d}", title="", status="draft",
            ticket_ids=[ticket_ids[index % count]], created_at="2024-01-01T00:00:00+00:00",
            updated_at="2024-01-01T00:00:00+00:00")
        sessions.save(session)
        if state != "draft":
            session = sessions.get(session.id)
            sessions.activate(session)
            if state == "completed":
                sessions.complete(sessions.get(session.id))
            elif state == "cancelled":
                sessions.cancel(sessions.get(session.id))

    ledger = BudgetLedger(project)
    ledger_count = min(LEDGER_RUN_COUNTS[-1], max(LEDGER_RUN_COUNTS[0], count))
    # Four explicit budgets make the state dimension material, even for smoke fixtures.
    budget_counts = {state: 0 for state in BUDGET_STATES}
    for state_index, state in enumerate(BUDGET_STATES):
        owner = ticket_ids[state_index]
        limits = {"tokens": 100000, "points": 1000, "runs": 100} if state == "active" else {"tokens": 0, "points": 0, "runs": 0}
        budget_id = ledger.create_budget("ticket", owner, limits=limits)
        budget_counts[state] += 1
        if state == "blocked_unknown":
            run_id = f"RUN-{seed % 100000:05d}-state-{state_index}"
            try:
                ledger.reserve(run_id, owner, None, {"tokens": 1, "points": 1, "runs": 1})
                ledger.start(run_id)
                ledger.finalize(run_id, "unknown", {"tokens": None, "points": None, "runs": 1})
            except Exception:
                pass
        elif state == "over_budget":
            # Keep the zero-limit budget and a failed reservation as observable evidence.
            try:
                ledger.reserve(f"RUN-{seed % 100000:05d}-state-{state_index}", owner, None, {"tokens": 1, "points": 1, "runs": 1})
            except Exception:
                pass
        assert ledger.get_budget(budget_id) is not None
    for index in range(ledger_count):
        owner = ticket_ids[index % count]
        budget_id = f"ticket:{owner}"
        if ledger.get_budget(budget_id) is None:
            ledger.create_budget("ticket", owner, limits={"tokens": 100000, "points": 1000, "runs": 100})
        run_id = f"RUN-{seed % 100000:05d}-{index:05d}"
        try:
            ledger.reserve(run_id, owner, None, {"tokens": 1, "points": 1, "runs": 1})
            if index % 4 == 0:
                ledger.start(run_id)
                ledger.finalize(run_id, "completed", {"tokens": 1, "points": 1, "runs": 1})
            elif index % 4 == 1:
                ledger.release(run_id)
        except Exception:
            pass

    logical = _logical_manifest(seed, size, storage_mode, statuses, ticket_ids, session_counts, ledger_count, budget_counts)
    manifest = {
        "schema_version": SCHEMA_VERSION, "source_kind": "synthetic", "seed": seed,
        "storage_mode": storage_mode, "hashes": {"manifest_sha256": logical["sha256"]},
        "counts": {"tickets": count, "sessions": session_count, "ledger_runs": ledger_count},
        "dimensions": {"size": size, "sizes": SIZES, "ticket_status": statuses,
                       "runs_per_ticket": list(RUNS_PER_TICKET), "session_counts": list(SESSION_COUNTS),
                       "ledger_run_counts": list(LEDGER_RUN_COUNTS), "session_states": session_counts,
                       "budget_states": budget_counts},
        # The logical checksum is stable across filesystem metadata and SQLite
        # page layout; the tree checksum is retained as provenance evidence.
        "fixture_files_sha256": logical["sha256"],
        "materialized_tree_sha256": _hash_tree(project), "redaction_policy": REDACTION_POLICY,
        "logical": logical["logical"],
    }
    validate_manifest(manifest)
    return manifest


def load_dataset(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid dataset manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported dataset manifest schema")
    validate_manifest(data)
    return data


def validate_manifest(manifest: dict[str, Any]) -> None:
    required = {"schema_version", "seed", "storage_mode", "counts", "dimensions", "hashes", "redaction_policy", "logical"}
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    counts = manifest["counts"]
    if any(not isinstance(counts.get(key), int) or counts[key] < 0 for key in ("tickets", "sessions", "ledger_runs")):
        raise ValueError("manifest counts must be non-negative integers")
    if manifest["storage_mode"] not in {"sqlite", "yaml"} or manifest["redaction_policy"] != REDACTION_POLICY:
        raise ValueError("invalid manifest storage or redaction policy")
    dimensions = manifest["dimensions"]
    if dimensions.get("runs_per_ticket") != list(RUNS_PER_TICKET) or dimensions.get("ledger_run_counts") != list(LEDGER_RUN_COUNTS):
        raise ValueError("required workload dimensions are incomplete")
    if dimensions.get("session_counts") != list(SESSION_COUNTS) or set(dimensions.get("budget_states", {})) != set(BUDGET_STATES):
        raise ValueError("required session/budget states are incomplete")
    if dimensions.get("ticket_status", {}).get("todo", 0) + sum(dimensions.get("ticket_status", {}).values()) == 0:
        raise ValueError("ticket state distribution is empty")


def manifest_for_dataset(path: Path, *, seed: int = 35527, size: str = "small", storage_mode: str = "sqlite") -> dict[str, Any]:
    return generate_fixture(path, seed=seed, size=size, storage_mode=storage_mode)
