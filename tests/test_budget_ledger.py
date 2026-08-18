from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vibe_orchestrator.budget_ledger import BudgetDenied, BudgetLedger


def test_reserve_is_idempotent_and_enforces_ticket_and_session(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 10, "points": 10, "runs": 1})
    ledger.create_budget("session", "SESSION-1", limits={"tokens": 10, "points": 10, "runs": 1})
    first = ledger.reserve("run-1", "DEL-1", "SESSION-1", {"tokens": 10, "points": 2, "runs": 1})
    second = ledger.reserve("run-1", "DEL-1", "SESSION-1", {"tokens": 10, "points": 2, "runs": 1})
    assert first.run_id == second.run_id
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["reserved"] == {"tokens": 10, "points": 2, "runs": 1}
    with pytest.raises(BudgetDenied):
        ledger.reserve("run-2", "DEL-1", "SESSION-1", {"tokens": 1, "points": 1, "runs": 1})


def test_finalize_release_and_unknown_are_idempotent(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": 100, "runs": 3})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    usage = {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12, "source": "provider"}
    ledger.finalize("run-1", "completed", usage)
    ledger.finalize("run-1", "completed", {"total_tokens": 99, "points": 99})
    run = ledger.get_run("run-1")
    assert run["state"] == "finalized"
    assert run["actual"]["total_tokens"] == 12
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["reserved"] == {"tokens": 0, "points": 0, "runs": 0}

    ledger.reserve("run-2", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.finalize("run-2", "unknown", {"input_tokens": None, "output_tokens": None})
    assert ledger.get_run("run-2")["state"] == "unknown"


def test_reconcile_absent_and_ambiguous_pending_runs(tmp_path: Path):
    ledger = BudgetLedger(tmp_path, pending_timeout=0)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 20, "points": 20, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.reserve("run-2", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    assert ledger.reconcile(evidence=lambda run: "absent" if run["run_id"] == "run-1" else "ambiguous") == ["run-1"]
    assert ledger.get_run("run-1")["state"] == "released"
    assert ledger.get_run("run-2")["recovery_marker"] == "ambiguous_start"


def test_concurrent_reserve_cannot_exceed_limit(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 10, "points": None, "runs": 1})

    def attempt(run_id):
        try:
            return ledger.reserve(run_id, "DEL-1", None, {"tokens": 10, "runs": 1}).run_id
        except BudgetDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("run-1", "run-2")))
    assert results.count(None) == 1


def test_terminal_run_can_only_be_corrected_by_append_only_adjustment(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 50, "points": 50, "runs": 5})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "points": 1, "runs": 1})
    ledger.finalize("run-1", "completed", {"total_tokens": 5, "points": 1, "points_status": "available"})
    adjustment_id = ledger.adjustment("run-1", {"tokens": 2, "points": 1, "runs": 0}, reason="provider correction", author="operator")
    assert adjustment_id == 1
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"]["tokens"] == 7
