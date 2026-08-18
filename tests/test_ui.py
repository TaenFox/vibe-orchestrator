import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import pytest

from vibe_orchestrator.control import DeliverySessionStore
from vibe_orchestrator.budget_ledger import BudgetLedger
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.ui import AUTO_REFRESH_SECONDS, AUTO_REFRESH_SCRIPT, CSS, render_board, render_board_fragment
from vibe_orchestrator.config import load_all_workflows


class _CardDetailsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.open_tags = []
        self.details_in_card = 0
        self.card_count = 0
        self.in_card = False
        self.card_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "card" in attrs.get("class", "").split():
            assert not self.in_card
            self.in_card = True
            self.card_count += 1
        if tag == "details":
            assert self.in_card
            self.details_in_card += 1
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.open_tags.append(tag)
            if self.in_card:
                self.card_depth += 1

    def handle_endtag(self, tag):
        assert self.open_tags and self.open_tags[-1] == tag
        self.open_tags.pop()
        if self.in_card:
            self.card_depth -= 1
        if tag == "details":
            self.details_in_card -= 1
        if self.in_card and self.card_depth == 0:
            assert self.details_in_card == 0
            self.in_card = False


def test_board_handles_long_text_and_preserves_keyboard_mobile_contract(project):
    store = TicketStore(project)
    title = "Заголовок <с переносом> " + ("очень-длинное-слово-" * 20)
    description = "Строка 1\n" + ("описание " * 400) + " <script>alert(1)</script>"
    store.create("discovery", "idea", title, description=description, status="ready")

    html = render_board(store, load_all_workflows(), "discovery")

    assert title.replace("<", "&lt;").replace(">", "&gt;") in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "setInterval(refresh" in html
    assert f"setInterval(refresh, {AUTO_REFRESH_SECONDS * 1000});" in html
    assert "@media (max-width:700px)" in html
    assert "*:focus-visible" not in html
    assert "focus-visible" in html
    assert '<textarea name="description"' in html


def test_board_keeps_details_balanced_between_multiple_cards(project):
    store = TicketStore(project)
    store.create("discovery", "idea", "Первая карточка", status="ready")
    store.create("discovery", "idea", "Вторая карточка", status="ready")

    page = render_board(store, load_all_workflows(), "discovery")
    parser = _CardDetailsParser()
    parser.feed(page)
    parser.close()

    assert parser.card_count == 2
    assert parser.details_in_card == 0
    assert parser.open_tags == []


def test_board_state_persists_scroll_details_and_form_values(project):
    store = TicketStore(project)
    ticket = store.create("discovery", "idea", "Состояние доски", description="Описание", status="ready")

    page = render_board(store, load_all_workflows(), "discovery")

    assert f'data-ticket-details="{ticket.id}"' in page
    assert "board?.scrollLeft" in page
    assert "refreshedBoard.scrollLeft = restored.boardScrollX" in page
    assert "detailsState" in page
    assert "data-ticket-details" in page
    assert "document.addEventListener('input'" in page
    assert "form?.getAttribute('action')" in page
    assert "window.location.reload" not in page


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


def test_budget_api_exposes_authoritative_snapshot_and_run_usage(http_server, project):
    store = TicketStore(project)
    ticket = store.create("delivery", "task", "Budget API", status="review")
    ledger = BudgetLedger(project)
    ledger.create_budget("ticket", ticket.id, limits={"tokens": 100, "points": 10, "runs": 2})
    ledger.reserve("run-budget", ticket.id, None, {"tokens": 20, "points": 1, "runs": 1})
    ledger.start("run-budget")
    ledger.finalize("run-budget", "completed", {
        "run_id": "run-budget", "model": "m", "reasoning_effort": "medium", "usage_ref": "u-1",
        "input_tokens": 7, "output_tokens": 5, "total_tokens": 12, "source": "provider",
        "captured_at": "2026-08-18T10:00:00+00:00", "normalization_version": "n.v1",
    })

    with urllib.request.urlopen(f"{http_server}/api/tickets") as response:
        item = next(value for value in json.load(response) if value["id"] == ticket.id)

    assert item["budget"]["limits"] == {"tokens": 100, "points": 10, "runs": 2}
    assert item["budget"]["spent"] == {"tokens": 12, "points": 1, "runs": 1}
    assert item["budget"]["snapshot_status"] == "fresh"
    assert item["budget"]["enforcement_state_exact"] is True
    assert item["budget_runs"][0]["source_confidence"] == "confirmed"
    assert item["budget_runs"][0]["actual"]["tokens"] == 12
    assert item["budget_runs"][0]["cost"] is None

    page = render_board(store, load_all_workflows(), "delivery")
    assert "budget active" in page
    assert "spent 12" in page
    assert "planned (tokens: 20 · points: 1 · runs: 1)" in page
    assert "reserved (tokens: 0 · points: 0 · runs: 0)" in page
    assert "actual (tokens: 12 · points: 1 · runs: 1)" in page
    assert "Attempt / ownership" in page
    assert "captured_at 2026-08-18T10:00:00+00:00" in page
    assert "normalization_version n.v1" in page
    assert "rate_card_version —" in page
    assert "cost —" in page
    assert "snapshot_status fresh" in page
    assert "enforcement_state_exact True" in page


def test_empty_delivery_session_ticket_selector_disables_add_action(http_server, project):
    sessions = DeliverySessionStore(project)
    session = sessions.create("Пустая сессия")

    page = render_board(TicketStore(project), load_all_workflows(), "delivery", session_store=sessions)

    assert 'value="" selected disabled>Нет доступных тикетов</option>' in page
    assert '<button disabled>Добавить тикет</button>' in page

    missing_ticket = urllib.request.Request(
        f"{http_server}/session/add",
        data=urllib.parse.urlencode({"session": session.id}).encode("utf-8"),
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(missing_ticket)
    assert error.value.code == 400
    assert "Тикет для добавления не выбран" in error.value.read().decode("utf-8")


def test_ticket_api_exposes_latest_usage_and_confirmed_aggregate(http_server, project):
    store = TicketStore(project)
    ticket = store.create("delivery", "task", "Token telemetry", status="done")
    store.record_run_event(ticket, run_id="run-1", stage_id="development", event="completed", token_usage={
        "input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
        "source": "codex_cli.turn.completed", "captured_at": "2026-08-17T10:00:00+00:00",
    })
    store.record_run_event(ticket, run_id="run-2", stage_id="development", event="completed", token_usage={
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
        "source": "unknown", "captured_at": None,
    })
    store.save(ticket)

    with urllib.request.urlopen(f"{http_server}/api/tickets") as response:
        payload = json.load(response)
    item = next(item for item in payload if item["id"] == ticket.id)

    assert item["token_usage"] == {
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
        "source": "unknown", "captured_at": None,
    }
    assert item["token_usage_aggregate"] == {
        "confirmed_runs": 0, "input_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "latest_captured_at": None,
    }


def test_ticket_api_aggregates_confirmed_usage_without_timestamp(http_server, project):
    store = TicketStore(project)
    ticket = store.create("delivery", "task", "Timestamp-free tokens", status="done")
    store.record_run_event(ticket, run_id="run-1", stage_id="development", event="completed", token_usage={
        "input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
        "source": "codex_cli.turn.completed", "captured_at": None,
    })
    store.save(ticket)

    with urllib.request.urlopen(f"{http_server}/api/tickets") as response:
        payload = json.load(response)
    item = next(item for item in payload if item["id"] == ticket.id)

    assert item["token_usage"]["total_tokens"] == 14
    assert item["token_usage"]["captured_at"] is None
    assert item["token_usage_aggregate"] == {
        "confirmed_runs": 0, "input_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "latest_captured_at": None,
    }


def test_ui_shows_unknown_timestamp_for_confirmed_usage(project):
    store = TicketStore(project)
    ticket = store.create("delivery", "task", "Timestamp-free tokens", status="done")
    store.record_run_event(ticket, run_id="run-1", stage_id="development", event="completed", token_usage={
        "input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
        "source": "codex_cli.turn.completed", "captured_at": None,
    })
    store.save(ticket)

    page = render_board(store, load_all_workflows(), "delivery")

    assert "Токены (актуальный источник)</span>unknown" in page


def test_ui_shows_unknown_usage_without_mixing_budget_or_cost(project):
    store = TicketStore(project)
    ticket = store.create("delivery", "task", "Unknown tokens", status="review")
    store.record_run_event(ticket, run_id="run-unknown", stage_id="review", event="completed", token_usage={
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
        "source": "unknown", "captured_at": None,
    })
    store.save(ticket)

    page = render_board(store, load_all_workflows(), "delivery")

    assert "Токены (актуальный источник)</span>unknown" in page
    assert "budget_points" not in page
    assert "cost" not in page


def test_ui_browser_behaviour_uses_partial_refresh_and_guards_input():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the inline browser behavior harness")

    script = re.search(r"<script>(.*?)</script>", AUTO_REFRESH_SCRIPT, re.DOTALL).group(1)
    harness = f"""
    (async () => {{
    const assert = require('node:assert/strict');
let timer;
    let fetches = 0;
    let openDetails = false;
    let active = null;
    globalThis.sessionStorage = {{ getItem: () => null, setItem: () => {{}} }};
    globalThis.location = {{ search: '?process=discovery' }};
    globalThis.setInterval = (callback, milliseconds) => {{ timer = {{callback, milliseconds}}; }};
    globalThis.window = {{ scrollTo: () => {{}} }};
    let changeHandler;
    globalThis.document = {{
      hidden: false,
      get activeElement() {{ return active; }},
      querySelector: (selector) => selector === 'details[open]' && openDetails ? {{}} : null,
      querySelectorAll: () => [],
      addEventListener: (event, handler) => {{ if (event === 'change') changeHandler = handler; }},
      scrollingElement: {{ scrollLeft: 0, scrollTop: 0 }}
    }};
    globalThis.fetch = async () => {{ fetches++; return {{ ok: true, text: async () => '<main class="board"></main>' }}; }};
{script}
assert.equal(timer.milliseconds, {AUTO_REFRESH_SECONDS * 1000});
    timer.callback();
    assert.equal(fetches, 1);
    await new Promise(resolve => setImmediate(resolve));
document.hidden = true;
timer.callback();
    assert.equal(fetches, 1);
document.hidden = false;
openDetails = true;
timer.callback();
    assert.equal(fetches, 1);
openDetails = false;
const input = {{ matches: (selector) => selector.includes('input'), value: 'введенный текст' }};
active = input;
timer.callback();
    assert.equal(fetches, 1);
assert.equal(document.activeElement.value, 'введенный текст');
changeHandler({{target: {{matches: (selector) => selector.includes('[data-board-search]'), value: 'поиск'}}}});
await new Promise(resolve => setImmediate(resolve));
assert.equal(fetches, 2);
active = null;
timer.callback();
await new Promise(resolve => setImmediate(resolve));
assert.equal(fetches, 3);
    }})();
"""
    completed = subprocess.run([node, "--eval", harness], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert "window.location.reload" not in AUTO_REFRESH_SCRIPT
    assert "/fragment?" in AUTO_REFRESH_SCRIPT
    assert "redirect: 'manual'" not in AUTO_REFRESH_SCRIPT
    assert "if (!response.ok) throw new Error('Не удалось создать тикет')" in AUTO_REFRESH_SCRIPT


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


def _post_create(base_url, fields):
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/create", data=payload, method="POST")
    try:
        return urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        return exc


def test_create_endpoint_validates_types_parent_and_special_characters(http_server, project):
    store = TicketStore(project)
    discovery = store.create("discovery", "idea", "Родитель <идея>")

    response = _post_create(http_server, {
        "process": "delivery", "type": "story", "title": "Задача <безопасно> & готово",
        "description": "Описание с <script>alert(1)</script> и ёлочными кавычками",
        "priority": "7", "parent": discovery.id,
    })
    assert response.status == 303
    created = next(ticket for ticket in TicketStore(project).list("delivery") if ticket.title.startswith("Задача"))
    assert created.description.startswith("Описание с <script>")
    assert created.priority == 7
    assert created.parent == discovery.id

    invalid_type = _post_create(http_server, {
        "process": "discovery", "type": "task", "title": "Недопустимый тип", "priority": "1",
    })
    assert invalid_type.code == 400
    assert "Недопустимый тип" in invalid_type.read().decode("utf-8")

    invalid_parent = _post_create(http_server, {
        "process": "delivery", "type": "story", "title": "Неверный parent", "priority": "1", "parent": "DEL-MISSING",
    })
    assert invalid_parent.code == 400
    assert "Parent тикет не найден" in invalid_parent.read().decode("utf-8")

    invalid_priority = _post_create(http_server, {
        "process": "delivery", "type": "task", "title": "Неверный приоритет", "priority": "-1",
    })
    assert invalid_priority.code == 400
    assert "неотрицательным" in invalid_priority.read().decode("utf-8")


def test_create_form_is_modal_and_offers_existing_compatible_parents(project):
    store = TicketStore(project)
    parent = store.create("discovery", "idea", "Parent <безопасно>")
    page = render_board(store, load_all_workflows(), "delivery")

    assert '<dialog id="create-ticket-dialog"' in page
    assert '<select name="type"' in page
    assert '<select name="parent"' in page
    assert f'value="{parent.id}"' in page
    assert 'data-process="delivery"' in page


def test_board_compact_groups_queue_and_agent_and_flat_filters(project):
    store = TicketStore(project)
    queued = store.create("delivery", "task", "В очереди", status="selected_for_session")
    active = store.create("delivery", "task", "В разработке", status="development")
    hidden = store.create("delivery", "task", "Другая стадия", status="review")

    compact = render_board(store, load_all_workflows(), "delivery")
    assert 'data-stage-group="selected_for_session"' in compact
    assert compact.index('data-stage="system_analysis"') < compact.index('data-stage="selected_for_session"')
    assert compact.index('data-stage="development"') < compact.index('data-stage="ready_for_development"')
    assert queued.title in compact and active.title in compact

    fragment = render_board_fragment(store, load_all_workflows(), "delivery")
    assert fragment.index('data-stage="system_analysis"') < fragment.index('data-stage="selected_for_session"')
    assert fragment.index('data-stage="development"') < fragment.index('data-stage="ready_for_development"')

    flat = render_board(store, load_all_workflows(), "delivery", mode="flat", search="разработке")
    assert 'class="board flat-list"' in flat
    assert active.title in flat
    assert queued.title not in flat and hidden.title not in flat


def test_ticket_drawer_contains_context_history_artifacts_and_accessibility(project):
    store = TicketStore(project)
    ticket = store.create("delivery", "task", "Полный контекст", description="Подробное описание", status="development", priority=7, parent="DEL-PARENT")
    ticket.last_summary = "Итог запуска"
    ticket.last_outcome = "completed"
    ticket.active_run = "run-active"
    ticket.blocked_by = ["DEL-BLOCKED"]
    ticket.retry_after = "2026-08-17T12:00:00+00:00"
    store.record_run_event(ticket, run_id="run-active", stage_id="development", event="started")
    store.save(ticket)

    page = render_board(store, load_all_workflows(), "delivery")

    assert 'class="card active-run"' in page
    assert 'data-open-ticket="' + ticket.id + '"' in page
    assert 'data-ticket-drawer' in page and 'data-drawer-ticket="' + ticket.id + '"' in page
    assert "Подробное описание" in page and "Итог запуска" in page
    assert "DEL-PARENT" in page and "DEL-BLOCKED" in page
    assert "/artifacts/run-active" in page
    assert "aria-label=\"Контекст тикета\"" in page
    assert "Escape" in page and "data-drawer-close" in page
    assert "fetch('/drawer?'" in page
    assert "event.key !== 'Tab'" in page
    assert "event.shiftKey" in page


def test_active_ticket_filter_keeps_only_running_tickets(project):
    store = TicketStore(project)
    active = store.create("delivery", "task", "В работе", status="development")
    active.active_run = "run-active"
    store.save(active)
    store.create("delivery", "task", "Ожидает", status="development")

    page = render_board(store, load_all_workflows(), "delivery", active="1")

    assert active.title in page
    assert "Ожидает" not in page
    assert 'data-board-active' in page
    assert 'value="1" selected>В работе' in page


def test_ui_fragment_endpoint_returns_only_board(http_server, project):
    store = TicketStore(project)
    ticket = store.create("discovery", "idea", "Найти меня", status="ready")
    with urllib.request.urlopen(f"{http_server}/fragment?process=discovery&mode=flat&search={ticket.id}") as response:
        fragment = response.read().decode("utf-8")
    assert response.status == 200
    assert fragment.startswith('<main class="board flat-list">')
    assert ticket.title in fragment
    assert "<!doctype html>" not in fragment


def test_ui_drawer_endpoint_returns_fresh_ticket_panel(http_server, project):
    store = TicketStore(project)
    ticket = store.create("delivery", "task", "Актуальный drawer", status="development")
    with urllib.request.urlopen(f"{http_server}/drawer?process=delivery&ticket={ticket.id}") as response:
        panel = response.read().decode("utf-8")
    assert response.status == 200
    assert panel.startswith('<section class="drawer-panel"')
    assert ticket.title in panel
    assert "<!doctype html>" not in panel
