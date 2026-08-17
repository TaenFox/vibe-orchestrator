import json
import re
import shutil
import subprocess
import urllib.request

import pytest

from vibe_orchestrator.control import DeliverySessionStore
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.ui import AUTO_REFRESH_SECONDS, AUTO_REFRESH_SCRIPT, CSS, render_board
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


def test_ui_api_payload_covers_discovery_delivery_and_active_session(http_server, project):
    store = TicketStore(project)
    discovery = store.create("discovery", "idea", "Discovery API", status="ready")
    delivery = store.create("delivery", "task", "Delivery API", status="review")
    delivery.active_run = "run-active"
    store.save(delivery)
    sessions = DeliverySessionStore(project)
    session = sessions.create("Регрессионная сессия")
    sessions.add(session.id, delivery.id, store)
    session = sessions.get(session.id)

    def get_json(path):
        with urllib.request.urlopen(f"{http_server}{path}") as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json; charset=utf-8"
            return json.load(response)

    tickets = get_json("/api/tickets")
    sessions = get_json("/api/sessions")
    session_payload = get_json(f"/api/sessions/{session.id}")

    assert {item["id"] for item in tickets} == {discovery.id, delivery.id}
    assert len(sessions) == 1
    assert session_payload["session"]["id"] == session.id
    assert {item["id"] for item in session_payload["tickets"]} == {delivery.id}
    assert session_payload["aggregate"]["active_run"] == 1
    assert session_payload["tickets"][0]["active_run"] == "run-active"


def test_ui_browser_behaviour_guards_refresh_and_keeps_focused_input():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the inline browser behavior harness")

    script = re.search(r"<script>(.*?)</script>", AUTO_REFRESH_SCRIPT, re.DOTALL).group(1)
    harness = f"""
const assert = require('node:assert/strict');
let timer;
let reloads = 0;
let openDetails = false;
let active = null;
globalThis.setInterval = (callback, milliseconds) => {{ timer = {{callback, milliseconds}}; }};
globalThis.window = {{ location: {{ reload: () => reloads++ }} }};
globalThis.document = {{
  hidden: false,
  get activeElement() {{ return active; }},
  querySelector: (selector) => selector === 'details[open]' && openDetails ? {{}} : null
}};
{script}
assert.equal(timer.milliseconds, {AUTO_REFRESH_SECONDS * 1000});
timer.callback();
assert.equal(reloads, 1);
document.hidden = true;
timer.callback();
assert.equal(reloads, 1);
document.hidden = false;
openDetails = true;
timer.callback();
assert.equal(reloads, 1);
openDetails = false;
const input = {{ matches: (selector) => selector.includes('input'), value: 'введенный текст' }};
active = input;
timer.callback();
assert.equal(reloads, 1);
assert.equal(document.activeElement.value, 'введенный текст');
active = null;
timer.callback();
assert.equal(reloads, 2);
"""
    completed = subprocess.run([node, "--eval", harness], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_ui_browser_contract_exposes_focusable_controls_and_mobile_column_width(project):
    store = TicketStore(project)
    store.create("discovery", "idea", "Keyboard and mobile", status="ready")
    page = render_board(store, load_all_workflows(), "discovery")

    assert re.search(r'<a href="/\?process=discovery">', page)
    assert re.search(r'<select name="process">', page)
    assert re.search(r'<input name="title"[^>]+required', page)
    assert re.search(r'<button(?: [^>]*)?>', page)

    mobile_rule = re.search(r'\.column\{width:min\(260px,calc\(100vw - 20px\)\);min-width:min\(260px,calc\(100vw - 20px\)\)\}', CSS)
    assert mobile_rule
    assert "@media (max-width:700px)" in CSS


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
