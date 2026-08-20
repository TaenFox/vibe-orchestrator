from pathlib import Path

from vibe_orchestrator.control import DeliverySessionStore
from vibe_orchestrator.framework_ui import _board_html, _resume_rework_if_requested, _session_detail_html, _sessions_html, _ticket_html, create_app
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.config import load_all_workflows


def test_framework_ui_builds_board_and_ticket_detail(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("discovery", "idea", "Framework ticket", description="Readable details", status="todo")
    workflows = load_all_workflows()

    board = _board_html(store, workflows, "discovery", worker_limit=0)
    detail = _ticket_html(store, workflows, ticket.id)

    assert "Framework ticket" in board
    assert ticket.id in board
    assert "Readable details" in detail
    assert "Создать тикет" in board
    assert 'name=delta' in board
    assert 'WIP' in board and 'Done' in board
    assert 'progress-step' in board
    assert 'Действия' in board
    assert f'/ticket/{ticket.id}/move' in board


def test_framework_ui_exposes_expected_routes(tmp_path: Path):
    paths = {route.path for route in create_app(tmp_path).routes}

    assert paths == {"/", "/healthz", "/ticket/{ticket_id}", "/new", "/sessions", "/sessions/new", "/sessions/{session_id}", "/sessions/{session_id}/{action}", "/tickets", "/tickets/reorder", "/ticket/{ticket_id}/move", "/ticket/{ticket_id}/resume-rework", "/ticket/{ticket_id}/recover-stale-run", "/workers"}


def test_framework_ui_attention_only_marks_explicit_human_action(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    review = store.create("delivery", "task", "Agent rework", status="review")
    review.last_outcome = "needs_rework"
    store.save(review)
    stopped = store.create("delivery", "rework", "Stopped rework", status="todo")
    stopped.blocked_reason = "rework_cycle_stopped"
    store.save(stopped)

    board = _board_html(store, load_all_workflows(), "delivery")

    assert board.count("<span class=attention-badge") == 1
    assert "Agent rework" in board
    assert "Stopped rework" in board


def test_framework_ui_shows_blocker_titles_on_board_and_detail(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    blocker = store.create("delivery", "task", "Незавершённая зависимость", status="review")
    ticket = store.create("delivery", "task", "Ожидающий тикет", status="development")
    ticket.blocked_by = [blocker.id]
    store.save(ticket)

    board = _board_html(store, load_all_workflows(), "delivery")
    detail = _ticket_html(store, load_all_workflows(), ticket.id)

    assert "ждёт зависимость" in board
    assert blocker.id in board and blocker.title in board
    assert "Блокировки" in detail
    assert blocker.title in detail


def test_manual_rework_resume_clears_only_cycle_block():
    ticket = type("Ticket", (), {"type": "rework", "blocked_reason": "rework_cycle_stopped"})()

    _resume_rework_if_requested(ticket, "selected_for_session")

    assert ticket.blocked_reason is None


def test_manual_rework_resume_does_not_clear_other_block():
    ticket = type("Ticket", (), {"type": "rework", "blocked_reason": "budget_exceeded_tokens"})()

    _resume_rework_if_requested(ticket, "selected_for_session")

    assert ticket.blocked_reason == "budget_exceeded_tokens"


def test_framework_ui_offers_explicit_rework_resume(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "rework", "Stopped rework", status="selected_for_session")
    ticket.blocked_reason = "rework_cycle_stopped"
    store.save(ticket)

    detail = _ticket_html(store, load_all_workflows(), ticket.id)

    assert f'/ticket/{ticket.id}/resume-rework' in detail
    assert "Разрешить ещё один проход реворка" in detail

    board = _board_html(store, load_all_workflows(), "delivery")
    assert f'/ticket/{ticket.id}/resume-rework' in board


def test_framework_ui_renders_session_list_and_membership(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Session ticket")
    sessions = DeliverySessionStore(tmp_path)
    session = sessions.create("Release session")
    sessions.add(session.id, ticket.id, store)

    listing = _sessions_html(sessions)
    detail = _session_detail_html(sessions, store, session.id)

    assert "Release session" in listing
    assert ticket.id in detail
    assert "Запустить сессию" in detail
