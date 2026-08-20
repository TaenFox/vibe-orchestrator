from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


DECISIONS = {"agree", "disagree"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_gate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("context.human_gate должен быть объектом")
    question = value.get("question")
    proposal = value.get("proposal")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("human_gate.question обязателен")
    if not isinstance(proposal, str) or not proposal.strip():
        raise ValueError("human_gate.proposal обязателен")
    gate = deepcopy(value)
    gate["question"] = question.strip()
    gate["proposal"] = proposal.strip()
    gate.setdefault("agree_label", "Согласиться")
    gate.setdefault("disagree_label", "Не согласиться")
    gate["status"] = "pending"
    return gate


def resolve_gate(ticket: Any, decision: str, *, actor: str = "owner") -> Any:
    if decision not in DECISIONS:
        raise ValueError("Решение должно быть agree или disagree")
    gate = ticket.context.get("human_gate") if isinstance(ticket.context, dict) else None
    if not isinstance(gate, dict) or gate.get("status") != "pending":
        raise ValueError("У тикета нет ожидающего решения владельца")
    resolved = deepcopy(gate)
    resolved.update({"status": "resolved", "decision": decision, "actor": actor, "decided_at": _now()})
    ticket.context["human_gate"] = resolved
    ticket.context_revision += 1
    ticket.blocked_reason = None
    ticket.last_outcome = f"human_decision_{decision}"
    ticket.last_summary = "Решение владельца: " + ("согласовано" if decision == "agree" else "отклонено")
    return ticket
