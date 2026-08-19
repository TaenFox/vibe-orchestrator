"""Deterministic, synthetic workload materialisation for the performance audit."""
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
REDACTION_POLICY = "synthetic identifiers and counts only; no non-empty titles, descriptions, prompts or raw payloads"


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _logical_manifest(seed: int, size: str, storage_mode: str, statuses: dict[str, int],
                      ticket_ids: list[str], session_counts: dict[str, int], ledger_count: int) -> dict[str, Any]:
    logical = {"schema_version": SCHEMA_VERSION, "seed": seed, "size": size, "storage_mode": storage_mode,
               "ticket_count": len(ticket_ids), "ticket_ids": ticket_ids, "ticket_status": statuses,
               "session_states": session_counts, "ledger_runs": ledger_count,
               "runs_per_ticket": [0, 1, 10], "budget_states": list(BUDGET_STATES)}
    return {"sha256": hashlib.sha256(json.dumps(logical, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "logical": logical}


def generate_fixture(project: Path, *, seed: int = 35527, size: str = "small",
                     storage_mode: str = "sqlite", include_legacy: bool = True) -> dict[str, Any]:
    """Materialise a redacted fixture.  All generated choices depend on ``seed``."""
    if size not in SIZES:
        raise ValueError(f"unknown size: {size}")
    if storage_mode not in {"sqlite", "yaml", "legacy"}:
        raise ValueError(f"unknown storage mode: {storage_mode}")
    storage_mode = "yaml" if storage_mode == "legacy" else storage_mode
    rng = random.Random(seed)
    project = Path(project)
    use_database = storage_mode == "sqlite"
    store = TicketStore(project, use_database=use_database)
    store.init()
    count = SIZES[size]
    # The ID permutation is deliberately seed-sensitive while remaining reproducible.
    order = list(range(count)); rng.shuffle(order)
    ticket_ids: list[str] = []
    statuses: dict[str, int] = {status: 0 for status in STATUSES}
    for position, source_index in enumerate(order):
        status = STATUSES[(source_index + rng.randrange(len(STATUSES))) % len(STATUSES)]
        ticket_id = f"FIX-{seed % 100000:05d}-{source_index:05d}"
        ticket_ids.append(ticket_id); statuses[status] += 1
        history_size = source_index % 11
        store.save(Ticket(id=ticket_id, process="delivery", type="story", title="", description="", status=status,
                          priority=1 + (source_index % 100), parent=ticket_ids[position - 1] if position and source_index % 17 == 0 else None,
                          blocked_by=[ticket_ids[position - 2]] if position > 1 and source_index % 29 == 0 else [],
                          run_history=[{"outcome": "completed", "attempt": n} for n in range(history_size)],
                          created_at="2024-01-01T00:00:00+00:00", updated_at="2024-01-01T00:00:00+00:00"))

    sessions = SessionStore(project, store, use_database=use_database); sessions.init()
    session_count = min(100, max(4, count // 10))
    session_counts = {state: 0 for state in SESSION_STATES}
    for index in range(session_count):
        state = SESSION_STATES[index % len(SESSION_STATES)]; session_counts[state] += 1
        session = DeliverySession(id=f"SESSION-{seed % 100000:05d}-{index:04d}", title="", status=state,
                                  ticket_ids=ticket_ids[index % count:index % count + min(10, count)],
                                  created_at="2024-01-01T00:00:00+00:00", updated_at="2024-01-01T00:00:00+00:00")
        if state == "active": session.started_at = session.created_at
        if state == "completed": session.started_at = session.completed_at = session.created_at
        if state == "cancelled": session.cancelled_at = session.created_at
        sessions.save(session)

    ledger = BudgetLedger(project)
    ledger_count = min(10000, max(100, count))
    for index in range(ledger_count):
        owner = ticket_ids[index % count]; budget_id = f"ticket:{owner}"
        if ledger.get_budget(budget_id) is None:
            ledger.create_budget("ticket", owner, limits={"tokens": 100000, "points": 1000, "runs": 100})
        run_id = f"RUN-{seed % 100000:05d}-{index:05d}"
        try:
            ledger.reserve(run_id, owner, None, {"tokens": 100, "points": 1, "runs": 1})
            if index % 4 == 0: ledger.start(run_id); ledger.finalize(run_id, "completed", {"tokens": 90, "points": 1, "runs": 1})
            elif index % 4 == 1: ledger.release(run_id)
        except Exception:
            pass
    manifest_hash = _logical_manifest(seed, size, storage_mode, statuses, ticket_ids, session_counts, ledger_count)
    manifest = {"schema_version": SCHEMA_VERSION, "source_kind": "synthetic", "seed": seed,
                "storage_mode": storage_mode, "hashes": {"manifest_sha256": manifest_hash["sha256"]},
                "counts": {"tickets": count, "sessions": session_count, "ledger_runs": ledger_count},
                "dimensions": {"size": size, "sizes": SIZES, "ticket_status": statuses,
                               "runs_per_ticket": [0, 1, 10], "session_counts": [1, 10, 100],
                               "ledger_run_counts": [100, 1000, 10000], "session_states": session_counts,
                               "budget_states": {state: 1 for state in BUDGET_STATES}},
                "fixture_files_sha256": manifest_hash["sha256"], "redaction_policy": REDACTION_POLICY}
    manifest["logical"] = manifest_hash["logical"]
    validate_manifest(manifest)
    return manifest


def load_dataset(path: Path) -> dict[str, Any]:
    """Load a previously exported manifest; silently ignoring ``--dataset`` is forbidden."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid dataset manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") not in {SCHEMA_VERSION, "performance-fixture.v1"}:
        raise ValueError("unsupported dataset manifest schema")
    if not isinstance(data.get("counts"), dict) or "tickets" not in data["counts"]:
        raise ValueError("dataset manifest is incomplete")
    if data.get("schema_version") == SCHEMA_VERSION:
        validate_manifest(data)
    return data


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate the public fixture contract without retaining ticket content."""
    required = {"schema_version", "seed", "storage_mode", "counts", "dimensions", "hashes", "redaction_policy"}
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    counts = manifest["counts"]
    if any(not isinstance(counts.get(key), int) for key in ("tickets", "sessions", "ledger_runs")):
        raise ValueError("manifest counts must be integers")
    if manifest["redaction_policy"] != REDACTION_POLICY:
        raise ValueError("unexpected fixture redaction policy")
    dimensions = manifest["dimensions"]
    if dimensions.get("runs_per_ticket") != [0, 1, 10] or dimensions.get("ledger_run_counts") != [100, 1000, 10000]:
        raise ValueError("required workload dimensions are incomplete")


def manifest_for_dataset(path: Path, *, seed: int = 35527, size: str = "small", storage_mode: str = "sqlite") -> dict[str, Any]:
    return generate_fixture(path, seed=seed, size=size, storage_mode=storage_mode)
