"""Deterministic, redacted workload generation for the performance audit."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from vibe_orchestrator.budget_ledger import BudgetLedger
from vibe_orchestrator.sessions import DeliverySession, SessionStore
from vibe_orchestrator.tickets import Ticket, TicketStore

SCHEMA_VERSION = "performance-fixture.v2"
SIZES = {"small": 100, "medium": 1000, "large": 5000, "xlarge": 10000}
PROFILE_DIMENSIONS = {
    "small": {"sessions": 4, "ledger_runs": 100},
    "medium": {"sessions": 10, "ledger_runs": 1000},
    "large": {"sessions": 100, "ledger_runs": 10000},
    "xlarge": {"sessions": 100, "ledger_runs": 10000},
}
# Valid delivery workflow stage IDs.  The fixture must not invent a ``blocked``
# stage because TicketStore/SessionStore validate status against the workflow.
STATUSES = ("todo", "selected_for_session", "system_analysis", "ready_for_development",
            "development", "ready_for_review", "review", "ready_for_acceptance",
            "acceptance", "ready_for_release", "release", "done")
SESSION_STATES = ("draft", "active", "completed", "cancelled")
BUDGET_STATES = ("active", "exhausted", "blocked_unknown", "over_budget")
RUNS_PER_TICKET = (0, 1, 10)
SESSION_COUNTS = (1, 10, 100)
LEDGER_RUN_COUNTS = (100, 1000, 10000)
REDACTION_POLICY = "synthetic identifiers and counts only; no non-empty titles, descriptions, prompts or raw payloads"


def _logical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _authoritative_paths(root: Path, storage_mode: str) -> list[Path]:
    vibe = root / ".vibe"
    if storage_mode == "sqlite":
        paths = [vibe / "control.sqlite3", vibe / "budgets" / "ledger.sqlite3"]
    else:
        paths = sorted((vibe / "tickets").glob("*/*.yaml"))
        paths += sorted((vibe / "sessions").glob("*.yaml"))
        paths.append(vibe / "budgets" / "ledger.sqlite3")
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"materialized store is missing: {missing[0].relative_to(root)}")
    return paths


def _checkpoint_sqlite(path: Path) -> None:
    try:
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        raise ValueError(f"cannot checkpoint materialized store: {path.name}") from exc


def _materialized_checksum(root: Path, storage_mode: str) -> str:
    paths = _authoritative_paths(root, storage_mode)
    for path in paths:
        if path.suffix == ".sqlite3":
            _checkpoint_sqlite(path)
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _budget_state_counts(ledger: BudgetLedger, ticket_ids: list[str]) -> dict[str, int]:
    counts = {state: 0 for state in BUDGET_STATES}
    for ticket_id in ticket_ids:
        budget = ledger.read_budget(f"ticket:{ticket_id}")
        if budget is None:
            raise ValueError(f"materialized ledger is missing budget: ticket:{ticket_id}")
        state = budget.get("status")
        if state not in counts:
            raise ValueError(f"unsupported materialized budget state: {state}")
        counts[state] += 1
    return counts


def _logical_manifest(seed: int, size: str, storage_mode: str, statuses: dict[str, int],
                     ticket_ids: list[str], session_counts: dict[str, int], ledger_count: int,
                     budget_counts: dict[str, int], run_counts: dict[str, int]) -> dict[str, Any]:
    logical = {
        "schema_version": SCHEMA_VERSION, "seed": seed, "size": size, "storage_mode": storage_mode,
        "ticket_count": len(ticket_ids), "ticket_ids": ticket_ids, "ticket_status": statuses,
        "session_states": session_counts, "ledger_runs": ledger_count, "runs_per_ticket": run_counts,
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
    run_counts = {str(value): 0 for value in RUNS_PER_TICKET}
    for position, source_index in enumerate(order):
        status = STATUSES[rng.randrange(len(STATUSES))]
        ticket_id = f"FIX-{seed % 100000:05d}-{source_index:05d}"
        ticket_ids.append(ticket_id)
        statuses[status] += 1
        run_count = RUNS_PER_TICKET[rng.randrange(len(RUNS_PER_TICKET))]
        run_counts[str(run_count)] += 1
        store.save(Ticket(
            id=ticket_id, process="delivery", type="story", title="", description="", status=status,
            priority=1 + rng.randrange(100),
            parent=ticket_ids[position - 1] if position and rng.random() < .06 else None,
            blocked_by=[ticket_ids[position - 2]] if position > 1 and rng.random() < .04 else [],
            run_history=[{"outcome": "completed", "attempt": n} for n in range(run_count)],
            created_at="2024-01-01T00:00:00+00:00", updated_at="2024-01-01T00:00:00+00:00"))

    sessions = SessionStore(project, store, use_database=use_database)
    sessions.init()
    session_count = PROFILE_DIMENSIONS[size]["sessions"]
    # Open sessions cannot contain workflow terminal tickets.
    session_ticket_ids = [ticket_id for ticket_id in ticket_ids
                          if statuses and store.get(ticket_id).status not in {"release", "done"}]
    if not session_ticket_ids:
        session_ticket_ids = ticket_ids
    session_counts = {state: 0 for state in SESSION_STATES}
    for index in range(session_count):
        state = SESSION_STATES[index % len(SESSION_STATES)]
        session_counts[state] += 1
        session = DeliverySession(
            id=f"SESSION-{seed % 100000:05d}{index:04d}", title="", status="draft",
            ticket_ids=[session_ticket_ids[index % len(session_ticket_ids)]], created_at="2024-01-01T00:00:00+00:00",
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
    ledger_count = PROFILE_DIMENSIONS[size]["ledger_runs"]
    # Four explicit budgets make the state dimension material, even for smoke fixtures.
    budget_counts = {state: 0 for state in BUDGET_STATES}
    for state_index, state in enumerate(BUDGET_STATES):
        owner = ticket_ids[state_index]
        limits = {"tokens": 100000, "points": 1000, "runs": 100}
        if state == "exhausted":
            # Keep points unbounded so the state is driven by token/run limits.
            limits = {"tokens": 1, "points": None, "runs": 1}
        elif state == "blocked_unknown":
            limits = {"tokens": 100, "points": 100, "runs": 100}
        elif state == "over_budget":
            limits = {"tokens": 1, "points": None, "runs": 1}
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
        elif state == "active":
            run_id = f"RUN-{seed % 100000:05d}-state-{state_index}"
            ledger.reserve(run_id, owner, None, {"tokens": 1, "points": 1, "runs": 1})
        elif state == "exhausted":
            try:
                run_id = f"RUN-{seed % 100000:05d}-state-{state_index}"
                ledger.reserve(run_id, owner, None, {"tokens": 1, "points": 1, "runs": 1})
                ledger.start(run_id)
                ledger.finalize(run_id, "completed", {
                    "run_id": run_id, "input_tokens": 1, "output_tokens": 0,
                    "total_tokens": 1, "source": "provider", "usage_ref": run_id,
                    "model": "synthetic", "reasoning_effort": "medium",
                    "captured_at": "2024-01-01T00:00:00+00:00",
                    "normalization_version": "tokens_per_1000.v1",
                })
            except Exception:
                pass
        elif state == "over_budget":
            # Actual usage is intentionally above the limit; this is not a denied
            # reservation and therefore leaves measurable over-budget evidence.
            try:
                run_id = f"RUN-{seed % 100000:05d}-state-{state_index}"
                ledger.reserve(run_id, owner, None, {"tokens": 1, "points": 1, "runs": 1})
                ledger.start(run_id)
                ledger.finalize(run_id, "completed", {
                    "run_id": run_id, "input_tokens": 2, "output_tokens": 0,
                    "total_tokens": 2, "source": "provider", "usage_ref": run_id,
                    "model": "synthetic", "reasoning_effort": "medium",
                    "captured_at": "2024-01-01T00:00:00+00:00",
                    "normalization_version": "tokens_per_1000.v1",
                })
            except Exception:
                pass
        assert ledger.get_budget(budget_id) is not None
    for index in range(max(0, ledger_count - 4)):
        owner = ticket_ids[4 + (index % max(1, count - 4))]
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

    budget_counts = _budget_state_counts(ledger, ticket_ids)
    actual_ledger_count = sum(len(ledger.list_runs(f"ticket:{ticket_id}")) for ticket_id in ticket_ids)
    logical = _logical_manifest(seed, size, storage_mode, statuses, ticket_ids, session_counts, actual_ledger_count, budget_counts, run_counts)
    materialized_checksum = ""
    manifest = {
        "schema_version": SCHEMA_VERSION, "source_kind": "synthetic", "seed": seed,
        "storage_mode": storage_mode, "hashes": {"manifest_sha256": logical["sha256"]},
        "counts": {"tickets": len(store.list("delivery")), "sessions": len(sessions.list()),
                    "ledger_runs": actual_ledger_count},
        "dimensions": {"size": size, "sizes": SIZES, "ticket_status": statuses,
                       "runs_per_ticket": list(RUNS_PER_TICKET), "runs_per_ticket_counts": run_counts,
                       "session_counts": list(SESSION_COUNTS),
                       "ledger_run_counts": list(LEDGER_RUN_COUNTS), "session_states": session_counts,
                       "profile_counts": PROFILE_DIMENSIONS[size],
                       "budget_states": budget_counts},
        # The logical checksum is stable across filesystem metadata and SQLite
        # page layout; the tree checksum is retained as provenance evidence.
        # Lifecycle APIs stamp wall-clock audit fields.  The logical checksum is
        # therefore the reproducible fixture identity; tree checksum remains
        # provenance for the actual YAML/SQLite materialization.
        "fixture_files_sha256": logical["sha256"],
        "logical_checksum": logical["sha256"], "materialized_tree_sha256": materialized_checksum,
        "redaction_policy": REDACTION_POLICY,
        "logical": logical["logical"],
    }
    # Run the read-back readers once before recording the final SQLite bytes;
    # opening the ledger performs idempotent schema bootstrap writes.
    _validate_materialized(manifest, project, check_checksum=False)
    manifest["materialized_tree_sha256"] = _materialized_checksum(project, storage_mode)
    validate_manifest(manifest, materialized_root=project)
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


def _validate_materialized(manifest: dict[str, Any], root: Path, *, check_checksum: bool = True) -> None:
    storage_mode = manifest["storage_mode"]
    # Check the bytes before opening BudgetLedger: its schema bootstrap is
    # intentionally allowed to maintain metadata, which must not become part
    # of the read-back result being checked.
    if check_checksum:
        actual_checksum = _materialized_checksum(root, storage_mode)
        if manifest["materialized_tree_sha256"] != actual_checksum:
            raise ValueError(
                "materialized checksum mismatch: "
                f"expected {manifest['materialized_tree_sha256']}, actual {actual_checksum}"
            )
    tickets = TicketStore(root, use_database=storage_mode == "sqlite").list("delivery")
    sessions = SessionStore(root, TicketStore(root, use_database=storage_mode == "sqlite"),
                            use_database=storage_mode == "sqlite").list()
    ledger = BudgetLedger(root)
    ticket_status = {status: 0 for status in STATUSES}
    run_counts = {str(value): 0 for value in RUNS_PER_TICKET}
    for ticket in tickets:
        if ticket.status not in ticket_status:
            raise ValueError(f"materialized ticket has unsupported status: {ticket.status}")
        ticket_status[ticket.status] += 1
        if str(len(ticket.run_history)) not in run_counts:
            raise ValueError(f"materialized ticket has unsupported run-history length: {ticket.id}")
        run_counts[str(len(ticket.run_history))] += 1
    session_states = {state: 0 for state in SESSION_STATES}
    for session in sessions:
        if session.status not in session_states:
            raise ValueError(f"materialized session has unsupported state: {session.status}")
        session_states[session.status] += 1
    actual_ledger_count = sum(len(ledger.list_runs(f"ticket:{ticket.id}")) for ticket in tickets)
    budget_states = _budget_state_counts(ledger, [ticket.id for ticket in tickets])
    actual_counts = {"tickets": len(tickets), "sessions": len(sessions), "ledger_runs": actual_ledger_count}
    if manifest["counts"] != actual_counts:
        raise ValueError(f"materialized counts mismatch: expected {manifest['counts']}, actual {actual_counts}")
    dimensions = manifest["dimensions"]
    actual_dimensions = {
        "ticket_status": ticket_status, "runs_per_ticket_counts": run_counts,
        "session_states": session_states, "budget_states": budget_states,
    }
    for field, actual in actual_dimensions.items():
        if dimensions.get(field) != actual:
            raise ValueError(f"materialized {field} mismatch: expected {dimensions.get(field)}, actual {actual}")


def validate_manifest(manifest: dict[str, Any], *, materialized_root: Path | None = None) -> None:
    required = {"schema_version", "source_kind", "seed", "storage_mode", "counts", "dimensions", "hashes",
                "fixture_files_sha256", "logical_checksum", "materialized_tree_sha256", "redaction_policy", "logical"}
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    counts = manifest["counts"]
    if any(not isinstance(counts.get(key), int) or counts[key] < 0 for key in ("tickets", "sessions", "ledger_runs")):
        raise ValueError("manifest counts must be non-negative integers")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["source_kind"] != "synthetic":
        raise ValueError("invalid manifest schema or source kind")
    if manifest["storage_mode"] not in {"sqlite", "yaml"} or manifest["redaction_policy"] != REDACTION_POLICY:
        raise ValueError("invalid manifest storage or redaction policy")
    dimensions = manifest["dimensions"]
    if dimensions.get("runs_per_ticket") != list(RUNS_PER_TICKET) or dimensions.get("ledger_run_counts") != list(LEDGER_RUN_COUNTS):
        raise ValueError("required workload dimensions are incomplete")
    if dimensions.get("session_counts") != list(SESSION_COUNTS) or set(dimensions.get("budget_states", {})) != set(BUDGET_STATES):
        raise ValueError("required session/budget states are incomplete")
    size = dimensions.get("size", manifest.get("logical", {}).get("size"))
    if size in PROFILE_DIMENSIONS:
        expected = PROFILE_DIMENSIONS[size]
        if dimensions.get("profile_counts") != expected:
            raise ValueError("profile dimensions do not match materialized fixture")
        if counts["sessions"] != expected["sessions"] or counts["ledger_runs"] != expected["ledger_runs"]:
            raise ValueError("manifest counts do not match materialized profile")
    if dimensions.get("sizes") != SIZES:
        raise ValueError("manifest size registry does not match fixture contract")
    run_counts = dimensions.get("runs_per_ticket_counts", {})
    if set(run_counts) != {str(value) for value in RUNS_PER_TICKET} or sum(run_counts.values()) != counts["tickets"]:
        raise ValueError("runs_per_ticket counts do not match materialized tickets")
    if sum(dimensions.get("ticket_status", {}).values()) != counts["tickets"]:
        raise ValueError("ticket status counts do not match materialized tickets")
    session_states = dimensions.get("session_states", {})
    if set(session_states) != set(SESSION_STATES) or any(session_states[state] < 1 for state in SESSION_STATES):
        raise ValueError("all session states must be materialized")
    budget_states = dimensions.get("budget_states", {})
    if any(budget_states[state] < 1 for state in BUDGET_STATES):
        raise ValueError("all budget states must be materialized")
    if sum(session_states.values()) != counts["sessions"]:
        raise ValueError("session state counts do not match materialized sessions")
    ticket_status = dimensions.get("ticket_status", {})
    if set(ticket_status) != set(STATUSES) or any(not isinstance(value, int) or value < 0 for value in ticket_status.values()):
        raise ValueError("ticket status counts are incomplete")
    if ticket_status.get("todo", 0) + sum(ticket_status.values()) == 0:
        raise ValueError("ticket state distribution is empty")
    logical = manifest["logical"]
    if manifest.get("logical_checksum") != manifest["hashes"].get("manifest_sha256"):
        raise ValueError("logical checksum aliases do not match")
    if manifest.get("fixture_files_sha256") != manifest["hashes"].get("manifest_sha256"):
        raise ValueError("fixture checksum must identify the logical fixture")
    if logical.get("schema_version") != SCHEMA_VERSION or logical.get("seed") != manifest["seed"] or logical.get("size") != size or logical.get("storage_mode") != manifest["storage_mode"]:
        raise ValueError("logical manifest dimensions do not match envelope")
    if logical.get("ticket_count") != counts["tickets"] or logical.get("session_states") != session_states or logical.get("budget_states") != budget_states:
        raise ValueError("logical manifest counts do not match materialized dimensions")
    if logical.get("ticket_status") != ticket_status or logical.get("runs_per_ticket") != run_counts or logical.get("ledger_runs") != counts["ledger_runs"]:
        raise ValueError("logical manifest workload dimensions do not match envelope")
    expected_hash = _logical_hash(logical) if isinstance(logical, dict) else None
    if manifest["hashes"].get("manifest_sha256") != expected_hash:
        raise ValueError("manifest logical checksum does not match logical payload")
    if materialized_root is not None:
        _validate_materialized(manifest, Path(materialized_root).resolve())


def manifest_for_dataset(path: Path, *, seed: int = 35527, size: str = "small", storage_mode: str = "sqlite") -> dict[str, Any]:
    return generate_fixture(path, seed=seed, size=size, storage_mode=storage_mode)
