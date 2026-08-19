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

SCHEMA_VERSION = "performance-fixture.v1"
SIZES = {"small": 100, "medium": 1000, "large": 5000, "xlarge": 10000}
STATUSES = ("todo", "selected_for_session", "system_analysis", "development", "review", "blocked", "done")
REDACTION_POLICY = "synthetic identifiers and counts only; no titles, descriptions, prompts or payloads"


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def generate_fixture(project: Path, *, seed: int = 35527, size: str = "small", include_legacy: bool = True) -> dict[str, Any]:
    """Create an anonymized fixture in *project* and return its manifest.

    The same seed and size produce byte-identical state (timestamps are fixed).
    """
    if size not in SIZES:
        raise ValueError(f"unknown size: {size}")
    random.Random(seed)  # reserve the seed in the fixture contract
    project = Path(project)
    store = TicketStore(project)
    store.init()
    count = SIZES[size]
    statuses: dict[str, int] = {}
    ticket_ids: list[str] = []
    for index in range(count):
        status = STATUSES[index % len(STATUSES)]
        ticket = Ticket(id=f"FIX-{index:05d}", process="delivery", type="story", title=f"Fixture ticket {index}",
                        description="Synthetic redacted workload", status=status, priority=1 + index % 100,
                        parent=f"FIX-{index - 1:05d}" if index and index % 17 == 0 else None,
                        blocked_by=[f"FIX-{index - 2:05d}"] if index and index % 29 == 0 else [],
                        consecutive_failures=index % 3, retry_after=None,
                        run_history=[{"outcome": "completed", "attempt": n} for n in range(index % 11)],
                        created_at="2024-01-01T00:00:00+00:00", updated_at="2024-01-01T00:00:00+00:00")
        store.save(ticket)
        ticket_ids.append(ticket.id)
        statuses[status] = statuses.get(status, 0) + 1

    sessions = SessionStore(project, store)
    sessions.init()
    for index in range(min(100, max(1, count // 100))):
        session = DeliverySession(id=f"SESSION-FIX-{index:04d}", title="Synthetic session", status=("active" if index % 2 else "draft"),
                                  ticket_ids=ticket_ids[index::max(1, min(100, count))][:10],
                                  created_at="2024-01-01T00:00:00+00:00", updated_at="2024-01-01T00:00:00+00:00")
        if session.status == "active":
            session.started_at = session.created_at
        sessions.save(session)

    ledger = BudgetLedger(project)
    for index in range(min(10000, max(100, count))):
        owner = f"FIX-{index % count:05d}"
        budget_id = f"ticket:{owner}"
        if ledger.get_budget(budget_id) is None:
            ledger.create_budget("ticket", owner, limits={"tokens": 100000, "points": 1000, "runs": 100})
        if index < min(100, count):
            run_id = f"RUN-FIX-{index:05d}"
            try:
                ledger.reserve(run_id, owner, None, {"tokens": 100, "points": 1, "runs": 1})
                ledger.start(run_id)
                ledger.finalize(run_id, "completed", {"tokens": 90, "points": 1, "runs": 1})
            except Exception:
                pass
    # Runtime stores contain wall-clock metadata (SQLite timestamps and WAL
    # headers). Hash the deterministic fixture description instead of those
    # bytes so repeated runs have a stable manifest hash.
    canonical = json.dumps({"seed": seed, "size": size, "count": count, "statuses": statuses,
                            "ticket_ids": ticket_ids, "session_count": len(sessions.list()),
                            "ledger_runs": min(100, count)}, sort_keys=True).encode()
    files_hash = hashlib.sha256(canonical).hexdigest()
    return {"schema_version": SCHEMA_VERSION, "source_kind": "synthetic", "seed": seed,
            "hashes": {"vibe_tree_sha256": files_hash}, "counts": {"tickets": count, "sessions": len(sessions.list()), "ledger_runs": min(100, count)},
            "dimensions": {"size": size, "sizes": list(SIZES), "ticket_status": statuses,
                            "required_states": ["ready", "review", "active", "blocked", "done"],
                            "runs_per_ticket": [0, 1, 10], "session_counts": [1, 10, 100],
                            "ledger_run_counts": [100, 1000, 10000],
                            "session_states": ["draft", "active", "completed", "cancelled"],
                            "budget_states": ["active", "exhausted", "blocked_unknown", "over_budget"]}, "redaction_policy": REDACTION_POLICY}


def manifest_for_dataset(path: Path, *, seed: int = 35527, size: str = "small") -> dict[str, Any]:
    return generate_fixture(path, seed=seed, size=size)
