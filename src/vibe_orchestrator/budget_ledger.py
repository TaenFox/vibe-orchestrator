"""Transactional Delivery budget ledger.

The ledger is deliberately independent from ticket/session YAML.  SQLite is
the source of truth for reservations and aggregates; callers may continue to
use YAML for lifecycle and traceability.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

DIMENSIONS = ("tokens", "points", "runs")
TERMINAL = {"finalized", "released", "unknown"}
STATES = {"reserved_pending_start", "started", *TERMINAL}


class BudgetDenied(RuntimeError):
    """A reservation cannot be admitted by one of the enforced scopes."""


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


def _add(left: int, right: int | None) -> int:
    return left + (right or 0)


@dataclass(frozen=True)
class Reservation:
    run_id: str
    state: str
    legacy: bool = False
    reason: str | None = None


class BudgetLedger:
    """SQLite-backed transactional ledger for ticket and session budgets."""

    def __init__(self, project: str | Path, *, timeout: float = 10.0, pending_timeout: float = 60.0):
        root = Path(project)
        self.path = root if root.suffix == ".sqlite3" else root / ".vibe" / "budgets" / "ledger.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.pending_timeout = pending_timeout
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
            INSERT OR IGNORE INTO metadata(key,value) VALUES ('schema_version','budget.v1');
            """)

    def create_budget(self, scope: str, owner_id: str, *, limits: Mapping[str, Any] | None = None,
                      budget_id: str | None = None, mode: str = "enforced") -> str:
        if scope not in {"ticket", "session"} or mode not in {"enforced", "legacy"}:
            raise ValueError("invalid budget scope or mode")
        limits = _values(limits)
        budget_id = budget_id or f"{scope}:{owner_id}"
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO budgets(budget_id,scope,owner_id,mode,limit_tokens,limit_points,limit_runs,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (budget_id, scope, owner_id, mode, limits["tokens"], limits["points"], limits["runs"], now, now))
        return budget_id

    def get_budget(self, budget_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM budgets WHERE budget_id=?", (budget_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["limits"] = {d: result[f"limit_{d}"] for d in DIMENSIONS}
        result["aggregates"] = {kind: {d: result[f"{kind}_{d}"] for d in DIMENSIONS} for kind in ("planned", "reserved", "finalized")}
        result["available"] = {d: None if result[f"limit_{d}"] is None else result[f"limit_{d}"] - sum(result[f"{k}_{d}"] for k in ("planned", "reserved", "finalized")) for d in DIMENSIONS}
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

    def _budget_rows(self, db: sqlite3.Connection, ticket_id: str, session_id: str | None):
        rows = []
        for scope, owner in (("ticket", ticket_id), ("session", session_id)):
            if owner:
                row = db.execute("SELECT * FROM budgets WHERE scope=? AND owner_id=?", (scope, owner)).fetchone()
                if row and row["mode"] == "enforced": rows.append(row)
        return rows

    def reserve(self, run_id: str, ticket_id: str, session_id: str | None, planned: Mapping[str, Any], *,
                attempt_kind: str = "initial", parent_run_id: str | None = None, parent_ticket_id: str | None = None) -> Reservation:
        planned_values = _values(planned)
        if planned_values["runs"] is None: planned_values["runs"] = 1
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                immutable = (existing["ticket_id"], existing["session_id"], existing["attempt_kind"], json.loads(existing["planned_json"]))
                if immutable != (ticket_id, session_id, attempt_kind, planned_values): raise ImmutableRunError("run_id parameters differ")
                return Reservation(run_id, existing["state"])
            rows = self._budget_rows(db, ticket_id, session_id)
            if not rows:
                return Reservation(run_id, "legacy", legacy=True)
            for row in rows:
                for d in DIMENSIONS:
                    if row[f"limit_{d}"] is not None and sum(row[f"{k}_{d}"] for k in ("planned", "reserved", "finalized")) + (planned_values[d] or 0) > row[f"limit_{d}"]:
                        raise BudgetDenied(f"{row['scope']} budget exceeded: {d}")
            now = _now()
            links = {"ticket": next((r["budget_id"] for r in rows if r["scope"] == "ticket"), None), "session": next((r["budget_id"] for r in rows if r["scope"] == "session"), None)}
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, ticket_id, session_id, links["ticket"], links["session"], attempt_kind, parent_run_id, parent_ticket_id, json.dumps(planned_values), json.dumps(planned_values), None, "reserved_pending_start", now, None, None, None))
            for row in rows:
                db.execute("UPDATE budgets SET reserved_tokens=reserved_tokens+?,reserved_points=reserved_points+?,reserved_runs=reserved_runs+?,updated_at=? WHERE budget_id=?", tuple(planned_values[d] or 0 for d in DIMENSIONS) + (now, row["budget_id"]))
            return Reservation(run_id, "reserved_pending_start", legacy=not rows)

    def _transition(self, run_id: str, state: str, actual: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            if row["state"] in TERMINAL: return self.get_run(run_id)  # idempotent
            held = json.loads(row["reserved_json"])
            now = _now()
            for budget_id in (row["ticket_budget_id"], row["session_budget_id"]):
                if budget_id:
                    db.execute("UPDATE budgets SET reserved_tokens=reserved_tokens-?,reserved_points=reserved_points-?,reserved_runs=reserved_runs-?,updated_at=? WHERE budget_id=?", tuple(held[d] or 0 for d in DIMENSIONS) + (now, budget_id))
            if state == "finalized":
                values = _values(actual)
                for budget_id in (row["ticket_budget_id"], row["session_budget_id"]):
                    if budget_id: db.execute("UPDATE budgets SET finalized_tokens=finalized_tokens+?,finalized_points=finalized_points+?,finalized_runs=finalized_runs+?,updated_at=? WHERE budget_id=?", tuple(values[d] or 0 for d in DIMENSIONS) + (now, budget_id))
            db.execute("UPDATE runs SET state=?,actual_json=?,terminal_at=? WHERE run_id=?", (state, json.dumps(actual) if actual is not None else None, now, run_id))
        return self.get_run(run_id)

    def start(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            if row["state"] == "reserved_pending_start": db.execute("UPDATE runs SET state='started',started_at=? WHERE run_id=?", (_now(), run_id))
        return self.get_run(run_id)

    def release(self, run_id: str) -> dict[str, Any]: return self._transition(run_id, "released")

    def finalize(self, run_id: str, terminal_outcome: str, usage: Mapping[str, Any]) -> dict[str, Any]:
        if terminal_outcome not in {"completed", "failed", "unknown"}: raise ValueError("invalid terminal outcome")
        actual = dict(usage)
        if "total_tokens" not in actual and actual.get("input_tokens") is not None and actual.get("output_tokens") is not None:
            actual["total_tokens"] = actual["input_tokens"] + actual["output_tokens"]
        if actual.get("tokens") is None:
            actual["tokens"] = actual.get("total_tokens")
        if actual.get("points") is None and actual.get("total_tokens") is not None: actual.update(normalize_budget_points(actual["total_tokens"], version=actual.get("normalization_version"), rate_card_version=actual.get("rate_card_version")))
        if actual.get("points") is None: actual["points_status"] = "unavailable"
        if terminal_outcome == "unknown" or actual.get("points_status") == "unavailable": return self._transition(run_id, "unknown", actual)
        return self._transition(run_id, "finalized", actual)

    def adjustment(self, run_id: str, delta: Mapping[str, Any], *, reason: str, author: str) -> int:
        if not reason or not author: raise ValueError("reason and author are required")
        values = _values(delta)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,ticket_budget_id,session_budget_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            if row["state"] not in TERMINAL: raise ValueError("adjustments require a terminal run")
            cursor = db.execute("INSERT INTO adjustments(adjusts_run_id,delta_json,reason,author,created_at) VALUES(?,?,?,?,?)", (run_id, json.dumps(values), reason, author, _now()))
            for budget_id in (row["ticket_budget_id"], row["session_budget_id"]):
                if budget_id: db.execute("UPDATE budgets SET finalized_tokens=finalized_tokens+?,finalized_points=finalized_points+?,finalized_runs=finalized_runs+?,updated_at=? WHERE budget_id=?", tuple(values[d] or 0 for d in DIMENSIONS) + (_now(), budget_id))
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
