from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vibe_orchestrator.budget_ledger import BudgetDenied, BudgetLedger


def confirmed(run_id: str, total: int, *, ref: str = "evt-1", input_tokens: int | None = None,
              output_tokens: int | None = None, model: str = "m"):
    input_tokens = total if input_tokens is None else input_tokens
    output_tokens = 0 if output_tokens is None else output_tokens
    return {
        "run_id": run_id, "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens, "model": model, "reasoning_effort": "medium",
        "source": "provider", "usage_ref": ref, "captured_at": "2026-08-17T10:00:00+00:00",
        "normalization_version": "tokens_per_1000.v1",
    }


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
    usage = confirmed("run-1", 12, input_tokens=7, output_tokens=5)
    ledger.finalize("run-1", "completed", usage)
    ledger.finalize("run-1", "completed", confirmed("run-1", 99, ref="evt-replay"))
    run = ledger.get_run("run-1")
    assert run["state"] == "finalized"
    assert run["actual"]["total_tokens"] == 12
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["reserved"] == {"tokens": 0, "points": 0, "runs": 0}

    ledger.reserve("run-2", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-2")
    ledger.finalize("run-2", "unknown", {"input_tokens": None, "output_tokens": None})
    assert ledger.get_run("run-2")["state"] == "unknown"


def test_provider_usage_without_run_correlation_or_ref_becomes_unknown(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": 10, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "completed", {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5, "source": "provider"})
    assert ledger.get_run("run-1")["state"] == "unknown"
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"]["runs"] == 0


def test_legacy_usage_never_finalizes_or_increases_aggregates(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": 10, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    legacy = {"run_id": "run-2", "input_tokens": 7, "output_tokens": 5, "total_tokens": 12,
              "source": "codex_cli.turn.completed", "usage_ref": "evt", "captured_at": "2026-08-17T10:00:00+00:00"}
    ledger.finalize("run-1", "completed", legacy)
    assert ledger.get_run("run-1")["state"] == "unknown"
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"] == {"tokens": 0, "points": 0, "runs": 0}


def test_runner_fallback_requires_policy_and_degraded_marker(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": 10, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    fallback = confirmed("run-1", 5)
    fallback.update(source="runner_fallback", fallback_policy_version="fallback.v1", degraded_confidence=True)
    ledger.finalize("run-1", "completed", fallback)
    assert ledger.get_run("run-1")["state"] == "finalized"

    ledger.reserve("run-2", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-2")
    invalid = dict(fallback)
    invalid.pop("fallback_policy_version")
    ledger.finalize("run-2", "completed", invalid)
    assert ledger.get_run("run-2")["state"] == "unknown"


def test_unknown_blocks_point_limited_scope_and_reserve(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": 10, "runs": 3})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "unknown", {"points": None, "points_status": "unavailable"})
    assert ledger.get_budget("ticket:DEL-1")["status"] == "blocked_unknown"
    with pytest.raises(BudgetDenied):
        ledger.reserve("run-2", "DEL-1", None, {"tokens": 1, "points": 1, "runs": 1})


def test_unknown_does_not_block_point_unlimited_scope_or_next_reserve(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": None, "runs": 3})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "unknown", {"points": None, "points_status": "unavailable"})

    assert ledger.get_budget("ticket:DEL-1")["status"] != "blocked_unknown"
    ledger.reserve("run-2", "DEL-1", None, {"tokens": 1, "points": 1, "runs": 1})
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["reserved"] == {
        "tokens": 1, "points": 1, "runs": 1,
    }


def test_unknown_blocks_zero_point_limit(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": 0, "runs": 3})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "runs": 1})
    assert ledger.get_budget("ticket:DEL-1")["status"] == "active"
    ledger.start("run-1")
    ledger.finalize("run-1", "unknown", {"points": None, "points_status": "unavailable"})

    assert ledger.get_budget("ticket:DEL-1")["status"] == "blocked_unknown"


def test_read_budget_derives_state_without_persisting(tmp_path: Path):
    ledger = BudgetLedger(tmp_path, authorizer=lambda **kwargs: (True, "test-policy"))
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 10, "points": 10, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "unknown", {"points": None, "source": "unknown"})
    ledger.increase_limit(actor="operator", target_scope="ticket", target_id="DEL-1", dimension="tokens",
                          delta=5, reason="review", reference="ref", expires_at="2999-01-01T00:00:00+00:00")
    with ledger._connect() as db:
        before = tuple(db.execute(
            "SELECT status, effective_limit_tokens, updated_at FROM budgets WHERE budget_id=?", ("ticket:DEL-1",)
        ).fetchone())
    snapshot = ledger.read_budget("ticket:DEL-1")
    with ledger._connect() as db:
        after = tuple(db.execute(
            "SELECT status, effective_limit_tokens, updated_at FROM budgets WHERE budget_id=?", ("ticket:DEL-1",)
        ).fetchone())
    assert snapshot["limits"]["tokens"] == 15
    assert snapshot["status"] == "blocked_unknown"
    assert after == before


@pytest.mark.parametrize(
    ("ticket_points", "session_points", "blocked_budget"),
    [(10, None, "ticket:DEL-1"), (None, 10, "session:SESSION-1")],
)
def test_unknown_blocks_only_point_limited_scope(tmp_path: Path, ticket_points, session_points, blocked_budget):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 100, "points": ticket_points, "runs": 3})
    ledger.create_budget("session", "SESSION-1", limits={"tokens": 100, "points": session_points, "runs": 3})
    ledger.reserve("run-1", "DEL-1", "SESSION-1", {"tokens": 10, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "unknown", {"points": None, "points_status": "unavailable"})

    budgets = (ledger.get_budget("ticket:DEL-1"), ledger.get_budget("session:SESSION-1"))
    assert next(budget for budget in budgets if budget["budget_id"] == blocked_budget)["status"] == "blocked_unknown"
    assert next(budget for budget in budgets if budget["budget_id"] != blocked_budget)["status"] != "blocked_unknown"


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


def test_concurrent_identical_reserve_has_one_run_and_one_aggregate_effect(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 10, "points": 10, "runs": 10})

    def attempt(_):
        return ledger.reserve("same-run", "DEL-1", None, {"tokens": 2, "points": 1, "runs": 1})

    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = list(pool.map(attempt, range(4)))
    assert {item.run_id for item in reservations} == {"same-run"}
    assert len(ledger.list_runs("ticket:DEL-1")) == 1
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["reserved"] == {"tokens": 2, "points": 1, "runs": 1}


def test_concurrent_reserve_checks_tokens_points_and_runs_atomically(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 10, "points": 5, "runs": 2})

    def attempt(index):
        try:
            ledger.reserve(f"run-{index}", "DEL-1", None, {"tokens": 6, "points": 3, "runs": 1})
            return True
        except BudgetDenied:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        accepted = list(pool.map(attempt, range(4)))
    budget = ledger.get_budget("ticket:DEL-1")
    assert sum(accepted) == 1
    assert budget["aggregates"]["reserved"] == {"tokens": 6, "points": 3, "runs": 1}
    assert len(ledger.list_runs("ticket:DEL-1")) == 1


def test_ticket_and_session_denial_leaves_no_partial_reservation(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 10, "points": 10, "runs": 2})
    ledger.create_budget("session", "SESSION-1", limits={"tokens": 1, "points": 1, "runs": 1})
    with pytest.raises(BudgetDenied):
        ledger.reserve("run-1", "DEL-1", "SESSION-1", {"tokens": 2, "points": 2, "runs": 1})
    assert ledger.get_run("run-1") is None
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["reserved"] == {"tokens": 0, "points": 0, "runs": 0}
    assert ledger.get_budget("session:SESSION-1")["aggregates"]["reserved"] == {"tokens": 0, "points": 0, "runs": 0}


def test_concurrent_terminal_transition_is_idempotent(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 50, "points": 50, "runs": 5})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "points": 1, "runs": 1})
    ledger.start("run-1")
    usage = confirmed("run-1", 5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ledger.finalize("run-1", "completed", usage), range(4)))
    assert {item["state"] for item in results} == {"finalized"}
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"] == {"tokens": 5, "points": 1, "runs": 1}


def test_terminal_run_can_only_be_corrected_by_append_only_adjustment(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 50, "points": 50, "runs": 5})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "completed", confirmed("run-1", 5))
    adjustment_id = ledger.adjustment("run-1", {"tokens": 2, "points": 1, "runs": 0}, reason="provider correction", author="operator")
    assert adjustment_id == 1
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"]["tokens"] == 7


def test_adjustment_accepts_signed_delta_and_rejects_underflow_atomically(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 50, "points": 50, "runs": 5})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "completed", confirmed("run-1", 5))
    ledger.adjustment("run-1", {"tokens": -1, "points": 0, "runs": 0}, reason="correction", author="operator")
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"]["tokens"] == 4
    with pytest.raises(ValueError):
        ledger.adjustment("run-1", {"tokens": -10, "points": 0, "runs": 0}, reason="bad correction", author="operator")
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"]["tokens"] == 4


def test_rework_reserves_parent_ticket_scope_but_keeps_child_run_metadata(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "PARENT", limits={"tokens": 10, "points": 10, "runs": 1})
    ledger.create_budget("ticket", "CHILD", limits={"tokens": 100, "points": 100, "runs": 100})

    ledger.reserve(
        "run-rework", "CHILD", None, {"tokens": 5, "points": 1, "runs": 1},
        attempt_kind="rework", parent_ticket_id="PARENT",
    )

    run = ledger.get_run("run-rework")
    assert run["ticket_id"] == "CHILD"
    assert run["parent_ticket_id"] == "PARENT"
    assert run["ticket_budget_id"] == "ticket:PARENT"
    assert ledger.get_budget("ticket:PARENT")["aggregates"]["reserved"] == {"tokens": 5, "points": 1, "runs": 1}
    assert ledger.get_budget("ticket:CHILD")["aggregates"]["reserved"] == {"tokens": 0, "points": 0, "runs": 0}


def test_finalize_requires_started_state(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 20, "points": 20, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "points": 1, "runs": 1})
    with pytest.raises(ValueError, match="invalid transition"):
        ledger.finalize("run-1", "completed", {"total_tokens": 5, "points": 1})
    assert ledger.get_run("run-1")["state"] == "reserved_pending_start"
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["reserved"] == {"tokens": 5, "points": 1, "runs": 1}
    ledger.start("run-1")
    ledger.finalize("run-1", "completed", {"total_tokens": 5, "points": 1})


def test_status_becomes_over_budget_after_finalize_overrun(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 5, "points": 10, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 2, "points": 1, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "completed", confirmed("run-1", 9))
    budget = ledger.get_budget("ticket:DEL-1")
    assert budget["status"] == "over_budget"
    assert budget["aggregates"]["finalized"] == {"tokens": 9, "points": 1, "runs": 1}


def test_status_becomes_exhausted_when_available_reaches_zero(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 5, "points": None, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "runs": 1})
    assert ledger.get_budget("ticket:DEL-1")["status"] == "exhausted"
    with pytest.raises(BudgetDenied):
        ledger.reserve("run-2", "DEL-1", None, {"tokens": 1, "runs": 1})


def test_unlimited_points_can_finalize_without_points(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 20, "points": None, "runs": 2})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "completed", confirmed("run-1", 5))
    run = ledger.get_run("run-1")
    assert run["state"] == "finalized"
    assert run["actual"]["points"] == 1
    assert ledger.get_budget("ticket:DEL-1")["status"] == "active"


def test_finalized_runs_counts_each_run_once(tmp_path: Path):
    ledger = BudgetLedger(tmp_path)
    ledger.create_budget("ticket", "DEL-1", limits={"tokens": 20, "points": None, "runs": 3})
    ledger.reserve("run-1", "DEL-1", None, {"tokens": 5, "runs": 1})
    ledger.start("run-1")
    ledger.finalize("run-1", "completed", confirmed("run-1", 5))
    ledger.finalize("run-1", "completed", confirmed("run-1", 99, ref="evt-replay"))
    assert ledger.get_budget("ticket:DEL-1")["aggregates"]["finalized"]["runs"] == 1
