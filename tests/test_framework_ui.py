from pathlib import Path

from vibe_orchestrator.framework_ui import _board_html, _ticket_html, create_app
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.config import load_all_workflows


def test_framework_ui_builds_board_and_ticket_detail(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("discovery", "idea", "Framework ticket", description="Readable details", status="todo")
    workflows = load_all_workflows()

    board = _board_html(store, workflows, "discovery")
    detail = _ticket_html(store, workflows, ticket.id)

    assert "Framework ticket" in board
    assert ticket.id in board
    assert "Readable details" in detail
    assert "Создать тикет" in board


def test_framework_ui_exposes_expected_routes(tmp_path: Path):
    paths = {route.path for route in create_app(tmp_path).routes}

    assert paths == {"/", "/healthz", "/ticket/{ticket_id}", "/new", "/tickets", "/ticket/{ticket_id}/move", "/workers"}
