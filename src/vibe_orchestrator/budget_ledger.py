"""Transactional Delivery budget ledger.

The ledger is deliberately independent from ticket/session YAML.  SQLite is
the source of truth for reservations and aggregates; callers may continue to
use YAML for lifecycle and traceability.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .token_usage import is_confirmed_token_usage

DIMENSIONS = ("tokens", "points", "runs")
TERMINAL = {"finalized", "released", "unknown"}
STATES = {"reserved_pending_start", "started", *TERMINAL}


class BudgetDenied(RuntimeError):
    """A reservation cannot be admitted by one of the enforced scopes."""

    def __init__(self, message: str, *, reason_code: str = "budget_denied"):
        super().__init__(message)
        self.reason_code = reason_code


class ImmutableRunError(ValueError):
    pass


def normalize_budget_points(total_tokens: int | None, *, version: str | None = None,
                            rate_card_version: str | None = None) -> dict[str, Any]:
    """Return versioned points without treating unknown usage as zero.

    The default policy is intentionally conservative and transparent: one
    point per 1,000 tokens, rounded up.  Deployments can pass their own
    version/rate-card policy or leave it unavailable.
    """
    if not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0:
        return {"points": None, "points_status": "unavailable", "normalization_version": None,
                "rate_card_version": None}
    version = version or "tokens_per_1000.v1"
    points = (total_tokens + 999) // 1000
    return {"points": points, "points_status": "available", "normalization_version": version,
            "rate_card_version": rate_card_version or version}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _values(value: Mapping[str, Any] | None) -> dict[str, int | None]:
    value = value or {}
    result = {}
    for name in DIMENSIONS:
        item = value.get(name)
        if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item < 0):
            raise ValueError(f"{name} must be a non-negative integer or null")
        result[name] = item
    return result


def _signed_values(value: Mapping[str, Any] | None) -> dict[str, int]:
    value = value or {}
    result = {}
    for name in DIMENSIONS:
        item = value.get(name, 0)
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"{name} must be an integer")
        result[name] = item
    return result


def _add(left: int, right: int | None) -> int:
    return left + (right or 0)


STATUS_PRECEDENCE = ("over_budget", "blocked_unknown", "stop_new_runs", "completed", "exhausted", "active")
PERMISSIONS = {
    "increase-limit": "budget.increase_limit",
    "allow-overrun": "budget.allow_overrun",
    "resolve-unknown": "budget.resolve_unknown",
}


@dataclass(frozen=True)
class Reservation:
    run_id: str
    state: str
    legacy: bool = False
    reason: str | None = None


class BudgetLedger:
    """SQLite-backed transactional ledger for ticket and session budgets."""

    def __init__(self, project: str | Path, *, timeout: float = 10.0, pending_timeout: float = 60.0,
                 clock: Callable[[], str] | None = None, authorizer: Callable[..., Any] | None = None):
        root = Path(project)
        self.path = root if root.suffix == ".sqlite3" else root / ".vibe" / "budgets" / "ledger.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.pending_timeout = pending_timeout
        self.clock = clock or _now
        self.authorizer = authorizer
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets (
              budget_id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('ticket','session')),
              owner_id TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('enforced','legacy')),
              status TEXT NOT NULL DEFAULT 'active', limit_tokens INTEGER CHECK(limit_tokens IS NULL OR limit_tokens >= 0),
              limit_points INTEGER CHECK(limit_points IS NULL OR limit_points >= 0), limit_runs INTEGER CHECK(limit_runs IS NULL OR limit_runs >= 0),
              planned_tokens INTEGER NOT NULL DEFAULT 0 CHECK(planned_tokens >= 0), planned_points INTEGER NOT NULL DEFAULT 0 CHECK(planned_points >= 0), planned_runs INTEGER NOT NULL DEFAULT 0 CHECK(planned_runs >= 0),
              reserved_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reserved_tokens >= 0), reserved_points INTEGER NOT NULL DEFAULT 0 CHECK(reserved_points >= 0), reserved_runs INTEGER NOT NULL DEFAULT 0 CHECK(reserved_runs >= 0),
              finalized_tokens INTEGER NOT NULL DEFAULT 0 CHECK(finalized_tokens >= 0), finalized_points INTEGER NOT NULL DEFAULT 0 CHECK(finalized_points >= 0), finalized_runs INTEGER NOT NULL DEFAULT 0 CHECK(finalized_runs >= 0),
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(scope, owner_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, session_id TEXT, ticket_budget_id TEXT,
              session_budget_id TEXT, attempt_kind TEXT NOT NULL, parent_run_id TEXT, parent_ticket_id TEXT,
              planned_json TEXT NOT NULL, reserved_json TEXT NOT NULL, actual_json TEXT, state TEXT NOT NULL CHECK(state IN ('reserved_pending_start','started','finalized','released','unknown')),
              reserved_at TEXT NOT NULL, started_at TEXT, terminal_at TEXT, recovery_marker TEXT,
              FOREIGN KEY(ticket_budget_id) REFERENCES budgets(budget_id), FOREIGN KEY(session_budget_id) REFERENCES budgets(budget_id)
            );
            CREATE TABLE IF NOT EXISTS adjustments (
              id INTEGER PRIMARY KEY AUTOINCREMENT, adjusts_run_id TEXT NOT NULL REFERENCES runs(run_id),
              delta_json TEXT NOT NULL, reason TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS budget_decisions (
              decision_id TEXT PRIMARY KEY,
              operation TEXT NOT NULL CHECK(operation IN ('increase-limit','allow-overrun','resolve-unknown')),
              actor TEXT NOT NULL, timestamp TEXT NOT NULL, reason TEXT NOT NULL,
              target_scope TEXT NOT NULL CHECK(target_scope IN ('ticket','session','run')),
              target_id TEXT NOT NULL, reference TEXT NOT NULL, policy TEXT NOT NULL,
              permission TEXT NOT NULL, payload_json TEXT NOT NULL,
              expires_at TEXT, one_shot INTEGER NOT NULL DEFAULT 0 CHECK(one_shot IN (0,1)),
              consumed_at TEXT, created_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO metadata(key,value) VALUES ('schema_version','budget.v1');
            INSERT OR IGNORE INTO metadata(key,value) VALUES ('decision_schema_version','budget_decisions.v1');
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(budgets)")}
            for dimension in DIMENSIONS:
                if f"base_limit_{dimension}" not in columns:
                    db.execute(f"ALTER TABLE budgets ADD COLUMN base_limit_{dimension} INTEGER")
                if f"effective_limit_{dimension}" not in columns:
                    db.execute(f"ALTER TABLE budgets ADD COLUMN effective_limit_{dimension} INTEGER")
                db.execute(
                    f"UPDATE budgets SET base_limit_{dimension}=limit_{dimension}, "
                    f"effective_limit_{dimension}=limit_{dimension} "
                    f"WHERE base_limit_{dimension} IS NULL AND limit_{dimension} IS NOT NULL"
                )
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('schema_version','budget.v2')")

    def _authorize(self, operation: str, *, actor: str, target_scope: str, target_id: str,
                   payload: Mapping[str, Any], reason: str, reference: str,
                   expires_at: str | None, one_shot: bool) -> str:
        permission = PERMISSIONS[operation]
        if not self.authorizer:
            raise PermissionError(f"no authorizer configured for {permission}")
        result = self.authorizer(actor=actor, operation=operation, permission=permission,
                                 target_scope=target_scope, target_id=target_id,
                                 dimensions=payload, reason=reason, reference=reference,
                                 expires_at=expires_at, one_shot=one_shot)
        if isinstance(result, tuple): allowed, policy = result
        elif isinstance(result, Mapping): allowed, policy = result.get("allow", False), result.get("policy")
        else: allowed, policy = bool(result), None
        if not allowed or not policy:
            raise PermissionError(f"policy denied {permission}")
        return str(policy)

    @staticmethod
    def _decision_fields(*, actor: str, reason: str, target_scope: str, target_id: str,
                         reference: str, expires_at: str | None, one_shot: bool) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (actor, reason, target_scope, target_id, reference)):
            raise ValueError("actor, reason, target scope/id and reference are required")
        if target_scope not in {"ticket", "session", "run"}:
            raise ValueError("invalid target scope")
        if (expires_at is None) == (not one_shot):
            raise ValueError("provide exactly one of expires_at or one_shot=true")

    def _insert_decision(self, db: sqlite3.Connection, *, decision_id: str, operation: str, actor: str,
                         reason: str, target_scope: str, target_id: str, reference: str,
                         policy: str, payload: Mapping[str, Any], expires_at: str | None,
                         one_shot: bool, timestamp: str) -> dict[str, Any]:
        record = {"decision_id": decision_id, "operation": operation, "actor": actor, "timestamp": timestamp,
                  "reason": reason, "target_scope": target_scope, "target_id": target_id,
                  "reference": reference, "policy": policy, "permission": PERMISSIONS[operation],
                  "payload": dict(payload), "expires_at": expires_at, "one_shot": one_shot,
                  "consumed_at": None}
        existing = db.execute("SELECT * FROM budget_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if existing:
            old = dict(existing); old["payload"] = json.loads(old.pop("payload_json"))
            old.pop("created_at", None); old["one_shot"] = bool(old["one_shot"])
            if any(old.get(key) != record.get(key) for key in ("decision_id","operation","actor","timestamp","reason","target_scope","target_id","reference","policy","permission","payload","expires_at","one_shot")):
                raise ValueError("decision_id already exists with different payload")
            return old
        db.execute("""INSERT INTO budget_decisions
          (decision_id,operation,actor,timestamp,reason,target_scope,target_id,reference,policy,permission,payload_json,expires_at,one_shot,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (decision_id, operation, actor, timestamp, reason, target_scope,
          target_id, reference, policy, PERMISSIONS[operation], json.dumps(payload, sort_keys=True), expires_at,
          int(one_shot), timestamp))
        return record

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["one_shot"] = bool(item["one_shot"])
        item.pop("created_at", None)
        return item

    def _refresh_effective_limits(self, db: sqlite3.Connection,
                                  budget_ids: list[str] | tuple[str, ...] | None = None,
                                  *, now: str | None = None) -> None:
        now = now or self.clock()
        query = "SELECT * FROM budgets"
        params: list[Any] = []
        if budget_ids:
            query += " WHERE budget_id IN (" + ",".join("?" for _ in budget_ids) + ")"
            params.extend(budget_ids)
        for row in db.execute(query, params).fetchall():
            effective = {}
            for dimension in DIMENSIONS:
                base = row[f"base_limit_{dimension}"]
                if base is None:
                    effective[dimension] = None
                    continue
                extra = 0
                for decision in self._active_decisions(db, "increase-limit", row["scope"], row["owner_id"], now=now):
                    payload = json.loads(decision["payload_json"])
                    if payload["dimension"] == dimension:
                        extra += payload["delta"]
                effective[dimension] = base + extra
            db.execute(
                "UPDATE budgets SET effective_limit_tokens=?, effective_limit_points=?, "
                "effective_limit_runs=?, updated_at=? WHERE budget_id=?",
                tuple(effective[d] for d in DIMENSIONS) + (now, row["budget_id"]),
            )

    def list_decisions(self, *, operation: str | None = None, target_scope: str | None = None,
                       target_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM budget_decisions WHERE 1=1"; params: list[Any] = []
        for column, value in (("operation", operation), ("target_scope", target_scope), ("target_id", target_id)):
            if value is not None: query += f" AND {column}=?"; params.append(value)
        with self._connect() as db: rows = db.execute(query + " ORDER BY timestamp, decision_id", params).fetchall()
        result = []
        for row in rows:
            item = dict(row); item["payload"] = json.loads(item.pop("payload_json")); item["one_shot"] = bool(item["one_shot"]); result.append(item)
        return result

    def _active_decisions(self, db: sqlite3.Connection, operation: str, scope: str, target_id: str, *, now: str) -> list[sqlite3.Row]:
        rows = db.execute("SELECT * FROM budget_decisions WHERE operation=? AND target_scope=? AND target_id=? AND (expires_at IS NULL OR expires_at>?) AND consumed_at IS NULL", (operation, scope, target_id, now)).fetchall()
        return rows

    def _record_decision(self, operation: str, *, decision_id: str | None, actor: str, reason: str,
                         target_scope: str, target_id: str, reference: str, payload: Mapping[str, Any],
                         expires_at: str | None, one_shot: bool) -> dict[str, Any]:
        self._decision_fields(actor=actor, reason=reason, target_scope=target_scope, target_id=target_id, reference=reference, expires_at=expires_at, one_shot=one_shot)
        if operation == "increase-limit" and target_scope == "run": raise ValueError("increase-limit requires ticket or session scope")
        if operation == "resolve-unknown" and target_scope != "run": raise ValueError("resolve-unknown requires run scope")
        decision_id = decision_id or str(uuid.uuid4())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM budget_decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if existing:
                old = self._decision_from_row(existing)
                requested = {"decision_id": decision_id, "operation": operation, "actor": actor,
                             "reason": reason, "target_scope": target_scope, "target_id": target_id,
                             "reference": reference, "payload": dict(payload), "expires_at": expires_at,
                             "one_shot": one_shot}
                if any(old.get(key) != value for key, value in requested.items()):
                    raise ValueError("decision_id already exists with different payload")
                return old
            if operation in {"increase-limit", "allow-overrun"} and target_scope in {"ticket", "session"}:
                if not db.execute("SELECT 1 FROM budgets WHERE scope=? AND owner_id=?", (target_scope, target_id)).fetchone():
                    raise KeyError(f"{target_scope}:{target_id}")
            if operation == "allow-overrun" and target_scope == "run":
                if not db.execute("SELECT 1 FROM runs WHERE run_id=?", (target_id,)).fetchone():
                    raise KeyError(target_id)
            if operation == "resolve-unknown":
                run = db.execute("SELECT state,ticket_budget_id,session_budget_id FROM runs WHERE run_id=?", (target_id,)).fetchone()
                if not run:
                    raise KeyError(target_id)
                if run["state"] != "unknown":
                    raise ValueError("resolve-unknown requires an unknown run")
            policy = self._authorize(operation, actor=actor, target_scope=target_scope, target_id=target_id,
                                     payload=payload, reason=reason, reference=reference,
                                     expires_at=expires_at, one_shot=one_shot)
            timestamp = self.clock()
            result = self._insert_decision(db, decision_id=decision_id, operation=operation, actor=actor, reason=reason, target_scope=target_scope, target_id=target_id, reference=reference, policy=policy, payload=payload, expires_at=expires_at, one_shot=one_shot, timestamp=timestamp)
            self._refresh_effective_limits(db, now=timestamp)
            if operation == "resolve-unknown":
                if one_shot:
                    db.execute("UPDATE budget_decisions SET consumed_at=? WHERE decision_id=?", (timestamp, decision_id))
                self._recompute_status(db, [value for value in (run["ticket_budget_id"], run["session_budget_id"]) if value], now=timestamp)
            return result

    def increase_limit(self, *, actor: str, target_scope: str, target_id: str, dimension: str, delta: int,
                       reason: str, reference: str, expires_at: str | None = None, one_shot: bool = False,
                       decision_id: str | None = None) -> dict[str, Any]:
        if dimension not in DIMENSIONS or not isinstance(delta, int) or isinstance(delta, bool) or delta < 0 or delta == 0:
            raise ValueError("dimension must be valid and delta must be a positive integer")
        return self._record_decision("increase-limit", actor=actor, reason=reason, target_scope=target_scope, target_id=target_id, reference=reference, payload={"dimension": dimension, "delta": delta}, expires_at=expires_at, one_shot=one_shot, decision_id=decision_id)

    def allow_overrun(self, *, actor: str, target_scope: str, target_id: str, dimensions: list[str] | tuple[str, ...],
                      reason: str, reference: str, expires_at: str | None = None, one_shot: bool = False,
                      decision_id: str | None = None) -> dict[str, Any]:
        if target_scope not in {"ticket", "session", "run"} or not dimensions or any(d not in DIMENSIONS for d in dimensions):
            raise ValueError("allow-overrun requires explicit valid dimensions and target")
        return self._record_decision("allow-overrun", actor=actor, reason=reason, target_scope=target_scope, target_id=target_id, reference=reference, payload={"dimensions": list(dict.fromkeys(dimensions))}, expires_at=expires_at, one_shot=one_shot, decision_id=decision_id)

    def resolve_unknown(self, *, actor: str, run_id: str, reason: str, reference: str,
                        estimate: Mapping[str, Any] | None = None, evidence: Mapping[str, Any] | None = None,
                        confidence: float | None = None, expires_at: str | None = None, one_shot: bool = False,
                        decision_id: str | None = None) -> dict[str, Any]:
        if not evidence and not (estimate and confidence is not None): raise ValueError("evidence or accepted estimate with confidence is required")
        if estimate and confidence is None: raise ValueError("estimate confidence is required")
        if confidence is not None and (isinstance(confidence, bool) or not 0 <= confidence <= 1): raise ValueError("confidence must be between 0 and 1")
        payload = {"mode": "evidence" if evidence else "estimate", "evidence": evidence, "estimate": estimate, "confidence": confidence}
        return self._record_decision("resolve-unknown", actor=actor, reason=reason, target_scope="run", target_id=run_id, reference=reference, payload=payload, expires_at=expires_at, one_shot=one_shot, decision_id=decision_id)

    def create_budget(self, scope: str, owner_id: str, *, limits: Mapping[str, Any] | None = None,
                      budget_id: str | None = None, mode: str = "enforced") -> str:
        if scope not in {"ticket", "session"} or mode not in {"enforced", "legacy"}:
            raise ValueError("invalid budget scope or mode")
        limits = _values(limits)
        budget_id = budget_id or f"{scope}:{owner_id}"
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO budgets(budget_id,scope,owner_id,mode,limit_tokens,limit_points,limit_runs,
                        base_limit_tokens,base_limit_points,base_limit_runs,effective_limit_tokens,effective_limit_points,effective_limit_runs,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (budget_id, scope, owner_id, mode,
                        limits["tokens"], limits["points"], limits["runs"], limits["tokens"], limits["points"], limits["runs"],
                        limits["tokens"], limits["points"], limits["runs"], now, now))
        return budget_id

    def set_status(self, budget_id: str, status: str) -> None:
        if status not in {"stop_new_runs", "completed"}:
            raise ValueError("invalid terminal budget status")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE budgets SET status=?,updated_at=? WHERE budget_id=?", (status, _now(), budget_id))

    def get_budget(self, budget_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            self._refresh_effective_limits(db, [budget_id], now=now)
            row = db.execute("SELECT * FROM budgets WHERE budget_id=?", (budget_id,)).fetchone()
            if row:
                self._recompute_status(db, [budget_id], now=now)
                row = db.execute("SELECT * FROM budgets WHERE budget_id=?", (budget_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["limits"] = {d: result[f"effective_limit_{d}"] for d in DIMENSIONS}
        result["aggregates"] = {kind: {d: result[f"{kind}_{d}"] for d in DIMENSIONS} for kind in ("planned", "reserved", "finalized")}
        result["available"] = {d: None if result[f"effective_limit_{d}"] is None else result[f"effective_limit_{d}"] - sum(result[f"{k}_{d}"] for k in ("planned", "reserved", "finalized")) for d in DIMENSIONS}
        return result

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("planned_json", "reserved_json", "actual_json"):
            result[key.removesuffix("_json")] = json.loads(result[key]) if result[key] else None
        return result

    def _budget_rows(self, db: sqlite3.Connection, budget_owner_ticket_id: str, session_id: str | None):
        rows = []
        for scope, owner in (("ticket", budget_owner_ticket_id), ("session", session_id)):
            if owner:
                row = db.execute("SELECT * FROM budgets WHERE scope=? AND owner_id=?", (scope, owner)).fetchone()
                if row and row["mode"] == "enforced": rows.append(row)
        return rows

    def _recompute_status(self, db: sqlite3.Connection, budget_ids: list[str] | tuple[str, ...], *, now: str | None = None) -> None:
        """Derive status from accounting while preserving explicit policy gates."""
        now = now or self.clock()
        self._refresh_effective_limits(db, budget_ids, now=now)
        for budget_id in dict.fromkeys(budget_ids):
            row = db.execute("SELECT * FROM budgets WHERE budget_id=?", (budget_id,)).fetchone()
            if not row or row["mode"] != "enforced":
                continue
            unknown = row["effective_limit_points"] is not None and db.execute(
                """SELECT 1 FROM runs r WHERE r.state='unknown' AND (r.ticket_budget_id=? OR r.session_budget_id=?)
                   AND NOT EXISTS (SELECT 1 FROM budget_decisions d WHERE d.operation='resolve-unknown'
                     AND d.target_scope='run' AND d.target_id=r.run_id
                     AND d.consumed_at IS NULL
                     AND (d.expires_at IS NULL OR d.expires_at>?)) LIMIT 1""",
                (budget_id, budget_id, now),
            ).fetchone() is not None
            over_budget = any(
                row[f"effective_limit_{dimension}"] is not None and row[f"finalized_{dimension}"] > row[f"effective_limit_{dimension}"]
                for dimension in DIMENSIONS
            )
            exhausted = any(
                row[f"effective_limit_{dimension}"] is not None and
                sum(row[f"{kind}_{dimension}"] for kind in ("planned", "reserved", "finalized")) > 0 and
                row[f"effective_limit_{dimension}"] - sum(row[f"{kind}_{dimension}"] for kind in ("planned", "reserved", "finalized")) == 0
                for dimension in DIMENSIONS
            )
            explicit = row["status"] if row["status"] in {"stop_new_runs", "completed"} else None
            status = "over_budget" if over_budget else "blocked_unknown" if unknown else explicit or "exhausted" if exhausted else "active"
            db.execute("UPDATE budgets SET status=?,updated_at=? WHERE budget_id=?", (status, now, budget_id))

    def reserve(self, run_id: str, ticket_id: str, session_id: str | None, planned: Mapping[str, Any], *,
                attempt_kind: str = "initial", parent_run_id: str | None = None, parent_ticket_id: str | None = None,
                budget_owner_ticket_id: str | None = None, require_session_budget: bool = False) -> Reservation:
        if attempt_kind == "rework" and not parent_ticket_id:
            raise BudgetDenied("rework requires parent_ticket_id for budget ownership")
        if attempt_kind == "rework" and budget_owner_ticket_id and budget_owner_ticket_id != parent_ticket_id:
            raise ValueError("rework budget owner must match parent_ticket_id")
        budget_owner_ticket_id = budget_owner_ticket_id or (
            parent_ticket_id if attempt_kind == "rework" else ticket_id
        )
        planned_values = _values(planned)
        if planned_values["runs"] is None: planned_values["runs"] = 1
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                immutable = (existing["ticket_id"], existing["session_id"], existing["attempt_kind"], json.loads(existing["planned_json"]))
                if immutable != (ticket_id, session_id, attempt_kind, planned_values): raise ImmutableRunError("run_id parameters differ")
                return Reservation(run_id, existing["state"])
            rows = self._budget_rows(db, budget_owner_ticket_id, session_id)
            if require_session_budget and not session_id:
                raise BudgetDenied("active session membership is required", reason_code="session_membership_required")
            if require_session_budget and not any(row["scope"] == "session" for row in rows):
                raise BudgetDenied("session budget record is missing", reason_code="budget_session_missing")
            if require_session_budget and not any(row["scope"] == "ticket" for row in rows):
                raise BudgetDenied("ticket budget record is missing", reason_code="budget_ticket_missing")
            if not rows:
                return Reservation(run_id, "legacy", legacy=True)
            budget_ids = [row["budget_id"] for row in rows]
            now = self.clock()
            self._recompute_status(db, budget_ids, now=now)
            rows = [db.execute("SELECT * FROM budgets WHERE budget_id=?", (budget_id,)).fetchone() for budget_id in budget_ids]
            for row in rows:
                if row["status"] in {"blocked_unknown", "completed", "stop_new_runs"}:
                    raise BudgetDenied(
                        f"{row['scope']} budget is {row['status']}",
                        reason_code=f"budget_{row['status']}",
                    )
                if row["status"] in {"exhausted", "over_budget"}:
                    has_increase = bool(self._active_decisions(db, "increase-limit", row["scope"], row["owner_id"], now=now))
                    has_overrun = bool(self._active_decisions(db, "allow-overrun", row["scope"], row["owner_id"], now=now) or self._active_decisions(db, "allow-overrun", "run", run_id, now=now))
                    if not (has_increase or has_overrun):
                        raise BudgetDenied(f"{row['scope']} budget is {row['status']}", reason_code=f"budget_{row['status']}")
                for d in DIMENSIONS:
                    allowed = row[f"effective_limit_{d}"]
                    if allowed is not None and sum(row[f"{k}_{d}"] for k in ("planned", "reserved", "finalized")) + (planned_values[d] or 0) > allowed:
                        bypass = []
                        for item in self._active_decisions(db, "allow-overrun", row["scope"], row["owner_id"], now=now):
                            bypass.extend(json.loads(item["payload_json"])["dimensions"])
                        run_bypass = self._active_decisions(db, "allow-overrun", "run", run_id, now=now)
                        bypass.extend(dimension for item in run_bypass for dimension in json.loads(item["payload_json"])["dimensions"])
                        if d in bypass:
                            continue
                        raise BudgetDenied(
                            f"{row['scope']} budget exceeded: {d}",
                            reason_code=f"budget_exceeded_{d}",
                        )
            links = {"ticket": next((r["budget_id"] for r in rows if r["scope"] == "ticket"), None), "session": next((r["budget_id"] for r in rows if r["scope"] == "session"), None)}
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, ticket_id, session_id, links["ticket"], links["session"], attempt_kind, parent_run_id, parent_ticket_id, json.dumps(planned_values), json.dumps(planned_values), None, "reserved_pending_start", now, None, None, None))
            for row in rows:
                db.execute("UPDATE budgets SET reserved_tokens=reserved_tokens+?,reserved_points=reserved_points+?,reserved_runs=reserved_runs+?,updated_at=? WHERE budget_id=?", tuple(planned_values[d] or 0 for d in DIMENSIONS) + (now, row["budget_id"]))
            consumed = []
            for row in rows:
                consumed.extend(self._active_decisions(db, "increase-limit", row["scope"], row["owner_id"], now=now))
                consumed.extend(self._active_decisions(db, "allow-overrun", row["scope"], row["owner_id"], now=now))
            consumed.extend(self._active_decisions(db, "allow-overrun", "run", run_id, now=now))
            for decision in consumed:
                if decision["one_shot"]:
                    db.execute("UPDATE budget_decisions SET consumed_at=? WHERE decision_id=? AND consumed_at IS NULL", (now, decision["decision_id"]))
            # Consumption changes effective limits/status; commit the refreshed state atomically.
            self._recompute_status(db, [row["budget_id"] for row in rows], now=now)
            return Reservation(run_id, "reserved_pending_start", legacy=not rows)

    def _transition(self, run_id: str, state: str, actual: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            if row["state"] in TERMINAL: return self.get_run(run_id)  # idempotent
            allowed = {"released": {"reserved_pending_start", "started"}, "finalized": {"started"}, "unknown": {"started"}}
            if row["state"] not in allowed.get(state, set()):
                raise ValueError(f"invalid transition: {row['state']} -> {state}")
            held = json.loads(row["reserved_json"])
            now = _now()
            budget_ids = [budget_id for budget_id in (row["ticket_budget_id"], row["session_budget_id"]) if budget_id]
            if state == "finalized":
                values = _values(actual)
                point_limited = any(
                    db.execute("SELECT 1 FROM budgets WHERE budget_id=? AND mode='enforced' AND limit_points IS NOT NULL", (budget_id,)).fetchone()
                    for budget_id in budget_ids
                )
                if values["points"] is None and point_limited:
                    state = "unknown"
            for budget_id in budget_ids:
                if budget_id:
                    db.execute("UPDATE budgets SET reserved_tokens=reserved_tokens-?,reserved_points=reserved_points-?,reserved_runs=reserved_runs-?,updated_at=? WHERE budget_id=?", tuple(held[d] or 0 for d in DIMENSIONS) + (now, budget_id))
            if state == "finalized":
                values = _values(actual)
                for budget_id in budget_ids:
                    if budget_id: db.execute("UPDATE budgets SET finalized_tokens=finalized_tokens+?,finalized_points=finalized_points+?,finalized_runs=finalized_runs+?,updated_at=? WHERE budget_id=?", tuple(values[d] or 0 for d in DIMENSIONS) + (now, budget_id))
            elif state == "unknown":
                # A point-limited scope cannot safely admit another run after
                # usage became unknown; the administrative override is out of
                # scope for this ledger.
                for budget_id in budget_ids:
                    if budget_id:
                        db.execute("UPDATE budgets SET updated_at=? WHERE budget_id=?", (now, budget_id))
            db.execute("UPDATE runs SET state=?,actual_json=?,terminal_at=? WHERE run_id=?", (state, json.dumps(actual) if actual is not None else None, now, run_id))
            self._recompute_status(db, budget_ids)
        return self.get_run(run_id)

    def start(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            if row["state"] == "reserved_pending_start": db.execute("UPDATE runs SET state='started',started_at=? WHERE run_id=?", (_now(), run_id))
            elif row["state"] in TERMINAL: return self.get_run(run_id)
            else: raise ValueError(f"invalid transition: {row['state']} -> started")
        return self.get_run(run_id)

    def release(self, run_id: str) -> dict[str, Any]: return self._transition(run_id, "released")

    def finalize(self, run_id: str, terminal_outcome: str, usage: Mapping[str, Any]) -> dict[str, Any]:
        if terminal_outcome not in {"completed", "failed", "unknown"}: raise ValueError("invalid terminal outcome")
        actual = dict(usage)
        # Usage is a fact, not a best-effort estimate.  Invalid provider
        # evidence is retained for audit but can only enter the unknown state.
        confirmed = is_confirmed_token_usage(actual, run_id=run_id)
        if not confirmed and terminal_outcome != "unknown":
            actual.setdefault("run_id", run_id)
            actual.setdefault("points", None)
            actual["points_status"] = "unavailable"
            actual["normalization_version"] = None
            return self._transition(run_id, "unknown", actual)
        if "total_tokens" not in actual and actual.get("input_tokens") is not None and actual.get("output_tokens") is not None:
            actual["total_tokens"] = actual["input_tokens"] + actual["output_tokens"]
        if actual.get("tokens") is None:
            actual["tokens"] = actual.get("total_tokens")
        if actual.get("points") is None and actual.get("total_tokens") is not None: actual.update(normalize_budget_points(actual["total_tokens"], version=actual.get("normalization_version"), rate_card_version=actual.get("rate_card_version")))
        if actual.get("points") is None: actual["points_status"] = "unavailable"
        if terminal_outcome == "unknown": return self._transition(run_id, "unknown", actual)
        # `runs` is a ledger event count, never a provider usage multiplier.
        actual["runs"] = 1
        return self._transition(run_id, "finalized", actual)

    def adjustment(self, run_id: str, delta: Mapping[str, Any], *, reason: str, author: str) -> int:
        if not reason or not author: raise ValueError("reason and author are required")
        values = _signed_values(delta)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,ticket_budget_id,session_budget_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            if row["state"] not in TERMINAL: raise ValueError("adjustments require a terminal run")
            budgets = [budget_id for budget_id in (row["ticket_budget_id"], row["session_budget_id"]) if budget_id]
            for budget_id in budgets:
                budget = db.execute("SELECT finalized_tokens,finalized_points,finalized_runs FROM budgets WHERE budget_id=?", (budget_id,)).fetchone()
                if any(budget[f"finalized_{dimension}"] + values[dimension] < 0 for dimension in DIMENSIONS):
                    raise ValueError("adjustment would make finalized aggregate negative")
            cursor = db.execute("INSERT INTO adjustments(adjusts_run_id,delta_json,reason,author,created_at) VALUES(?,?,?,?,?)", (run_id, json.dumps(values), reason, author, _now()))
            for budget_id in budgets:
                if budget_id: db.execute("UPDATE budgets SET finalized_tokens=finalized_tokens+?,finalized_points=finalized_points+?,finalized_runs=finalized_runs+?,updated_at=? WHERE budget_id=?", tuple(values[d] or 0 for d in DIMENSIONS) + (_now(), budget_id))
            self._recompute_status(db, budgets)
            return int(cursor.lastrowid)

    def reconcile(self, *, now: float | None = None, evidence: Callable[[dict[str, Any]], str] | None = None) -> list[str]:
        cutoff = (now if now is not None else time.time()) - self.pending_timeout
        changed = []
        with self._connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE state='reserved_pending_start'").fetchall()
        for row in rows:
            try: old = datetime.fromisoformat(row["reserved_at"]).timestamp()
            except ValueError: continue
            if old > cutoff: continue
            item = self.get_run(row["run_id"]); verdict = evidence(item) if evidence else "ambiguous"
            if verdict == "absent": self.release(row["run_id"]); changed.append(row["run_id"])
            elif verdict == "present": self.start(row["run_id"]); changed.append(row["run_id"])
            else:
                with self._connect() as update_db:
                    update_db.execute("BEGIN IMMEDIATE")
                    update_db.execute("UPDATE runs SET recovery_marker=? WHERE run_id=? AND state='reserved_pending_start'", ("ambiguous_start", row["run_id"]))
        return changed
