import json

from vibe_orchestrator.control import DeliverySessionStore
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.ui import AUTO_REFRESH_SECONDS, _session_payload, render_board
from vibe_orchestrator.config import load_all_workflows


def test_board_handles_long_text_and_preserves_keyboard_mobile_contract(project):
    store = TicketStore(project)
    title = "Заголовок <с переносом> " + ("очень-длинное-слово-" * 20)
    description = "Строка 1\n" + ("описание " * 400) + " <script>alert(1)</script>"
    store.create("discovery", "idea", title, description=description, status="ready")

    html = render_board(store, load_all_workflows(), "discovery")

    assert title.replace("<", "&lt;").replace(">", "&gt;") in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert f"setInterval(() => {{" in html
    assert f"}}, {AUTO_REFRESH_SECONDS * 1000});" in html
    assert "@media (max-width:700px)" in html
    assert "*:focus-visible" not in html
    assert "focus-visible" in html
    assert '<textarea name="description"' in html


def test_ui_api_payload_covers_discovery_delivery_and_active_session(project):
    store = TicketStore(project)
    discovery = store.create("discovery", "idea", "Discovery API", status="ready")
    delivery = store.create("delivery", "task", "Delivery API", status="review")
    delivery.active_run = "run-active"
    store.save(delivery)
    sessions = DeliverySessionStore(project)
    session = sessions.create("Регрессионная сессия")
    sessions.add(session.id, delivery.id, store)
    session = sessions.get(session.id)

    payload = _session_payload(session, store)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert {item["id"] for item in payload["tickets"]} == {delivery.id}
    assert payload["aggregate"]["active_run"] == 1
    assert payload["tickets"][0]["active_run"] == "run-active"
    assert discovery.id not in encoded


def test_ui_delivery_input_persists_long_text_and_is_rendered_safely(project):
    store = TicketStore(project)
    title = "Новый тикет <безопасно>"
    description = "длинный текст\n" * 100
    ticket = store.create("delivery", "task", title, description=description, priority=7, status="review")

    reloaded = TicketStore(project).get(ticket.id)
    html = render_board(TicketStore(project), load_all_workflows(), "delivery")
    assert reloaded.description == description
    assert reloaded.priority == 7
    assert "Новый тикет &lt;безопасно&gt;" in html
    assert "Новый тикет <безопасно>" not in html
