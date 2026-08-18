import pytest

from vibe_orchestrator.budget_ledger import BudgetDenied, BudgetLedger


def authorizer(**kwargs):
    return True, f"test-policy/{kwargs['permission']}"


def test_increase_limit_is_audited_and_applies_only_to_future_admission(tmp_path):
    ledger = BudgetLedger(tmp_path, authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"tokens": 5, "points": None, "runs": 2})
    ledger.reserve("old", "T", None, {"tokens": 5, "runs": 1})
    decision = ledger.increase_limit(actor="alice", target_scope="ticket", target_id="T", dimension="tokens", delta=5, reason="approved capacity", reference="INC-1", one_shot=True, decision_id="d1")
    ledger.reserve("new", "T", None, {"tokens": 5, "runs": 1})
    with pytest.raises(BudgetDenied):
        ledger.reserve("third", "T", None, {"tokens": 1, "runs": 1})
    assert ledger.get_budget("ticket:T")["aggregates"]["reserved"]["tokens"] == 10
    assert ledger.list_decisions()[0]["decision_id"] == decision["decision_id"]
    assert ledger.list_decisions()[0]["consumed_at"] is not None


def test_effective_limit_is_materialized_in_reads_and_expires(tmp_path):
    current = ["2026-08-18T00:00:00+00:00"]
    ledger = BudgetLedger(tmp_path, clock=lambda: current[0], authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"tokens": 5, "runs": 3})
    ledger.increase_limit(actor="a", target_scope="ticket", target_id="T", dimension="tokens", delta=5,
                          reason="temporary", reference="ref", expires_at="2026-08-18T01:00:00+00:00")
    assert ledger.get_budget("ticket:T")["limits"]["tokens"] == 10
    assert ledger.get_budget("ticket:T")["available"]["tokens"] == 10
    current[0] = "2026-08-18T02:00:00+00:00"
    assert ledger.get_budget("ticket:T")["limits"]["tokens"] == 5


def test_consumed_one_shot_resolve_keeps_unknown_unblocked(tmp_path):
    ledger = BudgetLedger(tmp_path, authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"points": 1, "runs": 3})
    ledger.reserve("run-a", "T", None, {"runs": 1})
    ledger.start("run-a")
    ledger.finalize("run-a", "unknown", {"points": None})
    ledger.resolve_unknown(actor="a", run_id="run-a", reason="r", reference="ref",
                          estimate={"points": 1}, confidence=0.8, one_shot=True, decision_id="resolve-1")
    assert ledger.list_decisions(operation="resolve-unknown")[0]["consumed_at"] is not None
    assert ledger.get_budget("ticket:T")["status"] != "blocked_unknown"


def test_expired_resolve_unknown_blocks_again(tmp_path):
    current = ["2026-08-18T00:00:00+00:00"]
    ledger = BudgetLedger(tmp_path, clock=lambda: current[0], authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"points": 1, "runs": 2})
    ledger.reserve("run-a", "T", None, {"runs": 1})
    ledger.start("run-a")
    ledger.finalize("run-a", "unknown", {"points": None})
    ledger.resolve_unknown(actor="a", run_id="run-a", reason="r", reference="ref",
                          evidence={"operator": "confirmed"}, expires_at="2026-08-18T01:00:00+00:00")
    assert ledger.get_budget("ticket:T")["status"] != "blocked_unknown"
    current[0] = "2026-08-18T02:00:00+00:00"
    assert ledger.get_budget("ticket:T")["status"] == "blocked_unknown"


def test_one_shot_increase_refreshes_effective_limit_after_consumption(tmp_path):
    ledger = BudgetLedger(tmp_path, authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"tokens": 5, "runs": 3})
    ledger.increase_limit(actor="a", target_scope="ticket", target_id="T", dimension="tokens", delta=5,
                          reason="temporary", reference="ref", one_shot=True, decision_id="limit-1")
    assert ledger.get_budget("ticket:T")["limits"]["tokens"] == 10
    ledger.reserve("run-a", "T", None, {"tokens": 5, "runs": 1})
    budget = ledger.get_budget("ticket:T")
    assert budget["limits"]["tokens"] == 5
    assert budget["available"]["tokens"] == 0
    assert budget["status"] == "exhausted"


def test_resolve_unknown_rejects_non_unknown_without_audit_row(tmp_path):
    ledger = BudgetLedger(tmp_path, authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"points": 1, "runs": 3})
    ledger.reserve("run-a", "T", None, {"runs": 1})
    with pytest.raises(ValueError, match="unknown run"):
        ledger.resolve_unknown(actor="a", run_id="run-a", reason="r", reference="ref",
                              evidence={"ticket": "operator"}, expires_at="2999-01-01T00:00:00+00:00")
    assert ledger.list_decisions() == []


def test_consumed_decision_replay_skips_authorizer_and_preserves_state(tmp_path):
    calls = []

    def counting_authorizer(**kwargs):
        calls.append(kwargs)
        return authorizer(**kwargs)

    ledger = BudgetLedger(tmp_path, authorizer=counting_authorizer)
    ledger.create_budget("ticket", "T", limits={"tokens": 5, "runs": 2})
    kwargs = dict(actor="a", target_scope="ticket", target_id="T", dimension="tokens", delta=5,
                  reason="r", reference="ref", one_shot=True, decision_id="once")
    first = ledger.increase_limit(**kwargs)
    ledger.reserve("run-a", "T", None, {"tokens": 10, "runs": 1})
    consumed = ledger.list_decisions()[0]["consumed_at"]
    second = ledger.increase_limit(**kwargs)
    assert second["timestamp"] == first["timestamp"]
    assert second["consumed_at"] == consumed
    assert len(calls) == 1


def test_permission_isolated_and_failed_decision_is_not_recorded(tmp_path):
    def deny_increase(**kwargs):
        return kwargs["permission"] != "budget.increase_limit", "policy/1"

    ledger = BudgetLedger(tmp_path, authorizer=deny_increase)
    ledger.create_budget("ticket", "T", limits={"tokens": 10})
    with pytest.raises(PermissionError):
        ledger.increase_limit(actor="a", target_scope="ticket", target_id="T", dimension="tokens", delta=1, reason="r", reference="ref", expires_at="2999-01-01T00:00:00+00:00")
    assert ledger.list_decisions() == []
    assert ledger.allow_overrun(actor="a", target_scope="ticket", target_id="T", dimensions=["tokens"], reason="r", reference="ref", expires_at="2999-01-01T00:00:00+00:00")["permission"] == "budget.allow_overrun"


def test_resolve_unknown_requires_evidence_or_confident_estimate_and_is_run_specific(tmp_path):
    ledger = BudgetLedger(tmp_path, authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"tokens": 10, "points": 1, "runs": 3})
    ledger.reserve("run-a", "T", None, {"tokens": 1, "runs": 1})
    ledger.start("run-a")
    ledger.finalize("run-a", "unknown", {"points": None})
    with pytest.raises(ValueError):
        ledger.resolve_unknown(actor="a", run_id="run-a", reason="r", reference="ref")
    ledger.resolve_unknown(actor="a", run_id="run-a", reason="estimated", reference="INC-2", estimate={"points": 1}, confidence=0.8, expires_at="2999-01-01T00:00:00+00:00")
    assert ledger.get_budget("ticket:T")["status"] != "blocked_unknown"
    assert ledger.get_run("run-a")["state"] == "unknown"
    assert ledger.list_decisions(operation="resolve-unknown")[0]["payload"]["mode"] == "estimate"


def test_decision_replay_is_idempotent_but_conflicting_payload_is_rejected(tmp_path):
    ledger = BudgetLedger(tmp_path, authorizer=authorizer)
    ledger.create_budget("ticket", "T", limits={"tokens": 10})
    kwargs = dict(actor="a", target_scope="ticket", target_id="T", dimension="tokens", delta=1, reason="r", reference="ref", expires_at="2999-01-01T00:00:00+00:00", decision_id="same")
    first = ledger.increase_limit(**kwargs)
    second = ledger.increase_limit(**kwargs)
    assert first["timestamp"] == second["timestamp"]
    assert len(ledger.list_decisions()) == 1
    with pytest.raises(ValueError):
        ledger.increase_limit(**{**kwargs, "delta": 2})
