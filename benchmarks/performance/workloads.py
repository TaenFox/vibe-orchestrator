"""Deterministic, redacted workload generation for the performance audit."""
from __future__ import annotations

import hashlib
import json
import random
import shutil
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

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
REDACTION_POLICY = "synthetic identifiers and redacted placeholder text only; no production titles, prompts or raw payloads"


def _logical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if ".git" in path.parts or path.name in {"control.sqlite3", "control.sqlite3-wal", "control.sqlite3-shm",
                                                   "ledger.sqlite3", "ledger.sqlite3-wal", "ledger.sqlite3-shm"}:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


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


def _canonical_manifest_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    logical = manifest.get("logical")
    dimensions = manifest.get("dimensions", {})
    counts = manifest.get("counts", {})
    return {
        "schema_version": manifest.get("schema_version"),
        "seed": manifest.get("seed"),
        "size": dimensions.get("size"),
        "storage_mode": manifest.get("storage_mode"),
        "ticket_count": counts.get("tickets"),
        "ticket_ids": logical.get("ticket_ids") if isinstance(logical, dict) else None,
        "ticket_status": dimensions.get("ticket_status"),
        "session_states": dimensions.get("session_states"),
        "ledger_runs": counts.get("ledger_runs"),
        "runs_per_ticket": dimensions.get("runs_per_ticket_counts"),
        "budget_states": dimensions.get("budget_states"),
    }


def manifest_identity(manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return the portable logical identity used for dataset equivalence."""
    payload = _canonical_manifest_payload(manifest)
    return payload, _logical_hash(payload)


def assert_manifest_identity(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    """Reject materialization that does not reproduce the supplied manifest."""
    expected_payload, expected_hash = manifest_identity(expected)
    actual_payload, actual_hash = manifest_identity(actual)
    supplied_hash = expected.get("hashes", {}).get("manifest_sha256")
    generated_hash = actual.get("hashes", {}).get("manifest_sha256")
    if (supplied_hash != expected_hash or generated_hash != actual_hash or
            expected_hash != actual_hash or expected_payload != actual_payload):
        differing_fields = sorted(
            key for key in set(expected_payload) | set(actual_payload)
            if expected_payload.get(key) != actual_payload.get(key)
        )
        suffix = f"; differing canonical fields={differing_fields}" if differing_fields else ""
        raise ValueError(
            "dataset identity mismatch: supplied="
            f"{supplied_hash or expected_hash}, materialized={generated_hash or actual_hash}{suffix}"
        )


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
        rich_payload = source_index != 0 and source_index % 25 == 0
        store.save(Ticket(
            id=ticket_id, process="delivery", type="story", title="", status=status,
            priority=1 + rng.randrange(100),
            parent=ticket_ids[position - 1] if position and rng.random() < .06 else None,
            blocked_by=[ticket_ids[position - 2]] if position > 1 and rng.random() < .04 else [],
            description=("[redacted synthetic description] " * 96).strip() if rich_payload else "",
            context={"redacted_context": [f"field-{n}" for n in range(32)]} if rich_payload else {},
            run_history=[{"outcome": "completed", "attempt": n,
                          "summary": "[redacted synthetic run summary]" if rich_payload else ""}
                         for n in range(run_count)],
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
    for state_index, state in enumerate(BUDGET_STATES):
        owner = ticket_ids[state_index]
        limits = {"tokens": 100000, "points": 1000, "runs": 100}
        if state == "exhausted":
            limits = {"tokens": 2, "points": 1, "runs": 1}
        elif state == "blocked_unknown":
            limits = {"tokens": 100, "points": 100, "runs": 100}
        elif state == "over_budget":
            limits = {"tokens": 1, "points": 1, "runs": 1}
        budget_id = ledger.create_budget("ticket", owner, limits=limits)
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
                ledger.finalize(run_id, "completed", {"run_id": run_id, "model": "fixture",
                    "reasoning_effort": "minimal", "usage_ref": run_id, "captured_at": "2024-01-01T00:00:00+00:00",
                    "source": "provider", "normalization_version": "synthetic.v1", "input_tokens": 1,
                    "output_tokens": 1, "total_tokens": 2, "tokens": 2, "points": 1, "runs": 1})
            except Exception:
                pass
        elif state == "over_budget":
            # Actual usage is intentionally above the limit; this is not a denied
            # reservation and therefore leaves measurable over-budget evidence.
            try:
                run_id = f"RUN-{seed % 100000:05d}-state-{state_index}"
                ledger.reserve(run_id, owner, None, {"tokens": 1, "points": 1, "runs": 1})
                ledger.start(run_id)
                ledger.finalize(run_id, "completed", {"run_id": run_id, "model": "fixture",
                    "reasoning_effort": "minimal", "usage_ref": run_id, "captured_at": "2024-01-01T00:00:00+00:00",
                    "source": "provider", "normalization_version": "synthetic.v1", "input_tokens": 1,
                    "output_tokens": 1, "total_tokens": 2, "tokens": 2, "points": 2, "runs": 1})
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
                ledger.finalize(run_id, "completed", {"run_id": run_id, "model": "fixture",
                    "reasoning_effort": "minimal", "usage_ref": run_id, "captured_at": "2024-01-01T00:00:00+00:00",
                    "source": "provider", "normalization_version": "synthetic.v1", "input_tokens": 1,
                    "output_tokens": 1, "total_tokens": 2, "tokens": 2, "points": 1, "runs": 1})
            elif index % 4 == 1:
                ledger.release(run_id)
        except Exception:
            pass

    actual_ledger_count = sum(len(ledger.list_runs(f"ticket:{ticket_id}")) for ticket_id in ticket_ids)
    ticket_budgets = ledger.read_budgets(scope="ticket", owner_ids=ticket_ids)
    if {budget["owner_id"] for budget in ticket_budgets} != set(ticket_ids):
        raise ValueError("fixture must materialize exactly one budget for every ticket")
    budget_counts = {state: 0 for state in BUDGET_STATES}
    for budget in ticket_budgets:
        status = budget["status"]
        if status not in budget_counts:
            raise ValueError(f"unsupported materialized budget state: {status}")
        budget_counts[status] += 1
    logical = _logical_manifest(seed, size, storage_mode, statuses, ticket_ids, session_counts, actual_ledger_count, budget_counts, run_counts)
    materialized_checksum = _hash_tree(project / ".vibe")
    readback = _readback_snapshot(project, storage_mode)
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
        "dataset_tree_sha256": materialized_checksum, "readback": readback,
        "redaction_policy": REDACTION_POLICY,
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


def _logical_manifest_hash(logical: dict[str, Any]) -> str:
    return _logical_hash(logical)


def _storage_snapshot(project: Path, storage_mode: str) -> dict[str, Any]:
    store = TicketStore(project, use_database=storage_mode == "sqlite")
    sessions = SessionStore(project, store, use_database=storage_mode == "sqlite")
    return {
        "tickets": {ticket.id: ticket.to_dict() for ticket in store.list()},
        "sessions": {session.id: session.to_dict() for session in sessions.list()},
    }


def _readback_snapshot(project: Path, storage_mode: str) -> dict[str, Any]:
    """Read materialized entities and authoritative ledger content."""
    snapshot = _storage_snapshot(project, storage_mode)
    ledger = BudgetLedger(project)
    with sqlite3.connect(ledger.path) as database:
        database.row_factory = sqlite3.Row
        budgets = [dict(row) for row in database.execute(
            "SELECT * FROM budgets WHERE scope = 'ticket' ORDER BY budget_id").fetchall()]
        runs = [dict(row) for row in database.execute(
            "SELECT * FROM runs ORDER BY reserved_at, run_id").fetchall()]
    for row in budgets + runs:
        for key in list(row):
            if key.endswith("_json"):
                encoded = row.pop(key)
                row[key[:-5]] = json.loads(encoded) if encoded is not None else None
    payload = {"tickets": snapshot["tickets"], "sessions": snapshot["sessions"],
               "budgets": budgets, "runs": runs}
    return {
        "digest": _logical_hash(payload),
        "counts": {"tickets": len(payload["tickets"]), "sessions": len(payload["sessions"]),
                    "budgets": len(budgets), "ledger_runs": len(runs)},
        "ticket_ids": sorted(payload["tickets"]), "session_ids": sorted(payload["sessions"]),
        "ledger_owners": [list(item) for item in sorted(
            (row["run_id"], row["ticket_id"], row.get("ticket_budget_id")) for row in runs)],
        "budget_statuses": {row["budget_id"]: row["status"] for row in budgets},
    }


def _convert_storage(project: Path, source_storage: str, target_storage: str) -> dict[str, Any]:
    """Convert control-plane materialization without regenerating the dataset."""
    if source_storage == target_storage:
        proof = _readback_snapshot(project, source_storage)
        return {"equivalent": True, "source": proof, "target": proof}
    before = _readback_snapshot(project, source_storage)
    entity_snapshot = _storage_snapshot(project, source_storage)
    vibe = Path(project) / ".vibe"
    database = vibe / "control.sqlite3"
    for sidecar in (database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")):
        sidecar.unlink(missing_ok=True)
    if target_storage == "yaml":
        shutil.rmtree(vibe / "tickets", ignore_errors=True)
        shutil.rmtree(vibe / "sessions", ignore_errors=True)
        tickets = TicketStore(project, use_database=False)
        tickets.init()
        for payload in entity_snapshot["tickets"].values():
            ticket = Ticket.from_dict(payload)
            tickets.ticket_path(ticket).parent.mkdir(parents=True, exist_ok=True)
            tickets.ticket_path(ticket).write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
        sessions = SessionStore(project, tickets, use_database=False)
        sessions.init()
        for payload in entity_snapshot["sessions"].values():
            session = DeliverySession.from_dict(payload)
            sessions.session_path(session).parent.mkdir(parents=True, exist_ok=True)
            sessions.session_path(session).write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    else:
        from vibe_orchestrator.control_db_migration import migrate_control_plane
        migrate_control_plane(project, database)
    after = _readback_snapshot(project, target_storage)
    equivalent = before["digest"] == after["digest"]
    if not equivalent:
        raise ValueError("alternate storage conversion changed dataset contents")
    return {"equivalent": True,
            "source": before, "target": after}


def materialize_dataset(project: Path, dataset_path: Path, manifest: dict[str, Any], *,
                        storage_mode: str | None = None) -> dict[str, Any]:
    """Copy an approved dataset bundle into the isolated benchmark project.

    A dataset is a directory (or a manifest JSON next to one) containing the
    materialized ``.vibe`` tree.  The manifest describes that tree; it is not a
    recipe for generating a replacement fixture.
    """
    source = Path(dataset_path).resolve()
    root = source if source.is_dir() else source.parent
    source_vibe = root / ".vibe"
    if not source_vibe.is_dir():
        raise ValueError("dataset must contain a materialized .vibe directory")
    target_vibe = Path(project) / ".vibe"
    if target_vibe.exists():
        shutil.rmtree(target_vibe)
    shutil.copytree(source_vibe, target_vibe)
    materialized = dict(manifest)
    materialized["source_kind"] = "approved_dataset"
    materialized["dataset_root"] = str(root)
    materialized["materialized_tree_sha256"] = _hash_tree(target_vibe)
    expected = manifest.get("dataset_tree_sha256")
    if source.is_dir() and (not expected or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise ValueError("approved dataset must declare dataset_tree_sha256")
    if expected != materialized["materialized_tree_sha256"]:
        raise ValueError("dataset materialized tree checksum does not match manifest")
    materialized["dataset_tree_sha256"] = materialized["materialized_tree_sha256"]
    readback = _readback_snapshot(project, manifest["storage_mode"])
    if readback != manifest.get("readback"):
        raise ValueError("dataset read-back reconciliation does not match manifest")
    materialized["readback"] = readback
    if storage_mode is not None and storage_mode != manifest["storage_mode"]:
        conversion = _convert_storage(project, manifest["storage_mode"], storage_mode)
        materialized["storage_mode"] = storage_mode
        logical = dict(materialized["logical"])
        logical["storage_mode"] = storage_mode
        materialized["logical"] = logical
        materialized["hashes"] = {"manifest_sha256": _logical_manifest_hash(logical)}
        materialized["logical_checksum"] = materialized["hashes"]["manifest_sha256"]
        materialized["fixture_files_sha256"] = materialized["hashes"]["manifest_sha256"]
        materialized["materialized_tree_sha256"] = _hash_tree(target_vibe)
        materialized["dataset_tree_sha256"] = materialized["materialized_tree_sha256"]
        materialized["readback"] = _readback_snapshot(project, storage_mode)
        materialized["storage_conversion"] = conversion
        validate_manifest(materialized)
    return materialized


def validate_manifest(manifest: dict[str, Any]) -> None:
    required = {"schema_version", "source_kind", "seed", "storage_mode", "counts", "dimensions", "hashes",
                "fixture_files_sha256", "logical_checksum", "materialized_tree_sha256", "dataset_tree_sha256",
                "redaction_policy", "logical", "readback"}
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    counts = manifest["counts"]
    if any(not isinstance(counts.get(key), int) or counts[key] < 0 for key in ("tickets", "sessions", "ledger_runs")):
        raise ValueError("manifest counts must be non-negative integers")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["source_kind"] not in {"synthetic", "approved_dataset"}:
        raise ValueError("invalid dataset source or schema")
    if not isinstance(manifest["seed"], int) or manifest["storage_mode"] not in {"sqlite", "yaml"} or manifest["redaction_policy"] != REDACTION_POLICY:
        raise ValueError("invalid manifest storage or redaction policy")
    dimensions = manifest["dimensions"]
    if dimensions.get("runs_per_ticket") != list(RUNS_PER_TICKET) or dimensions.get("ledger_run_counts") != list(LEDGER_RUN_COUNTS):
        raise ValueError("required workload dimensions are incomplete")
    if dimensions.get("session_counts") != list(SESSION_COUNTS) or set(dimensions.get("budget_states", {})) != set(BUDGET_STATES):
        raise ValueError("required session/budget states are incomplete")
    size = dimensions.get("size")
    if size not in PROFILE_DIMENSIONS:
        raise ValueError("manifest must declare a supported profile size")
    if size in PROFILE_DIMENSIONS:
        expected = PROFILE_DIMENSIONS[size]
        if dimensions.get("profile_counts") != expected:
            raise ValueError("profile dimensions do not match materialized fixture")
        if counts["sessions"] != expected["sessions"] or counts["ledger_runs"] != expected["ledger_runs"]:
            raise ValueError("manifest counts do not match materialized profile")
        if counts["tickets"] != SIZES[size]:
            raise ValueError(
                "manifest ticket count does not match profile: "
                f"size={size}, expected={SIZES[size]}, actual={counts['tickets']}"
            )
    run_counts = dimensions.get("runs_per_ticket_counts", {})
    if set(run_counts) != {str(value) for value in RUNS_PER_TICKET} or sum(run_counts.values()) != counts["tickets"]:
        raise ValueError("runs_per_ticket counts do not match materialized tickets")
    if sum(dimensions.get("ticket_status", {}).values()) != counts["tickets"]:
        raise ValueError("ticket status counts do not match materialized tickets")
    session_states = dimensions.get("session_states", {})
    if set(session_states) != set(SESSION_STATES) or any(session_states[state] < 1 for state in SESSION_STATES):
        raise ValueError("all session states must be materialized")
    budget_states = dimensions.get("budget_states", {})
    if (set(budget_states) != set(BUDGET_STATES) or
            any(isinstance(budget_states[state], bool) or not isinstance(budget_states[state], int) or
                budget_states[state] < 0 for state in BUDGET_STATES) or
            sum(budget_states.values()) != counts["tickets"]):
        raise ValueError("budget state counts do not match materialized tickets")
    if any(budget_states[state] < 1 for state in BUDGET_STATES):
        raise ValueError("all budget states must be materialized")
    if sum(session_states.values()) != counts["sessions"]:
        raise ValueError("session state counts do not match materialized sessions")
    if dimensions.get("ticket_status", {}).get("todo", 0) + sum(dimensions.get("ticket_status", {}).values()) == 0:
        raise ValueError("ticket state distribution is empty")
    logical = manifest["logical"]
    expected_hash = _logical_hash(logical) if isinstance(logical, dict) else None
    if manifest["hashes"].get("manifest_sha256") != expected_hash:
        raise ValueError("manifest logical checksum does not match logical payload")
    statuses = dimensions.get("ticket_status", {})
    if set(statuses) != set(STATUSES) or sum(statuses.values()) != counts["tickets"]:
        raise ValueError("ticket status counts do not match materialized tickets")
    ticket_ids = manifest["logical"].get("ticket_ids")
    if (not isinstance(ticket_ids, list) or len(ticket_ids) != counts["tickets"] or
            len(set(ticket_ids)) != len(ticket_ids) or
            any(not isinstance(item, str) or not re.fullmatch(r"FIX-\d{5}-\d{5}", item) for item in ticket_ids)):
        raise ValueError("manifest ticket ids are not synthetic and materialized")
    expected_logical = _canonical_manifest_payload(manifest)
    if manifest["logical"] != expected_logical:
        raise ValueError("manifest logical payload does not match materialized fields")
    logical_hash = _logical_hash(expected_logical)
    if not isinstance(manifest["hashes"], dict) or manifest["hashes"].get("manifest_sha256") != logical_hash:
        raise ValueError("manifest checksum mismatch")
    if manifest["logical_checksum"] != logical_hash or manifest["fixture_files_sha256"] != logical_hash:
        raise ValueError("logical fixture checksum mismatch")
    if not isinstance(manifest["materialized_tree_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["materialized_tree_sha256"]):
        raise ValueError("invalid materialized tree checksum")
    if not isinstance(manifest["dataset_tree_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["dataset_tree_sha256"]):
        raise ValueError("invalid dataset tree checksum")
    readback = manifest["readback"]
    if not isinstance(readback, dict) or not re.fullmatch(r"[0-9a-f]{64}", readback.get("digest", "")):
        raise ValueError("invalid materialized read-back proof")


def manifest_for_dataset(path: Path, *, seed: int = 35527, size: str = "small", storage_mode: str = "sqlite") -> dict[str, Any]:
    return generate_fixture(path, seed=seed, size=size, storage_mode=storage_mode)
