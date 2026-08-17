from pathlib import Path

from vibe_orchestrator.cli import build_parser
from vibe_orchestrator.config import load_all_workflows
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.ui import render_board


def test_cli_help_is_localized():
    help_text = build_parser().format_help()
    assert "Pull-оркестратор тикетов Codex" in help_text
    assert "Создать тикет" in help_text
    assert "Запустить оркестратор" in help_text


def test_workflow_titles_are_localized():
    workflows = load_all_workflows()

    assert workflows["delivery"].title == "Доставка"
    assert workflows["delivery"].by_id["ready_for_review"].title == "Готово к ревью"
    assert workflows["process_management"].by_id["auto_acceptance"].title == "Авто-приемка"


def test_generated_vibe_readme_is_localized(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()

    readme = (tmp_path / ".vibe" / "README.md").read_text(encoding="utf-8")
    assert "Состояние тикетов для vibe-orchestrator" in readme
    assert "`runs/` содержит локальные артефакты запусков" in readme
    assert "`run.json`, `events.jsonl`, `result.json`" in readme


def test_ui_board_uses_russian_labels(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Локализация интерфейса", description="Показать детали тикета", status="ready_for_review")
    ticket.wip_exempt = True
    ticket.blocked_by = ["DEL-LOCK"]
    ticket.active_run = "run-1"
    ticket.parent = "DEL-PARENT"
    ticket.last_outcome = "failed"
    ticket.last_summary = "Проверка перевода"
    store.save(ticket)

    html = render_board(store, load_all_workflows(), "delivery")
    assert "Готово к ревью" in html
    assert "заблокирован: 1" in html
    assert "агент выполняется" in html
    assert "без учета WIP" in html
    assert "приоритет 100" in html
    assert "setInterval(() => {" in html
    assert "document.querySelector('details[open]')" in html
    assert "автообновление 5с, пауза при открытых деталях" in html
    assert "Подробнее" in html
    assert "Показать детали тикета" in html
    assert "Родитель" in html
    assert "DEL-PARENT" in html
    assert "Блокирует" in html
    assert "DEL-LOCK" in html
    assert "Последний outcome" in html
    assert "failed" in html


def test_ui_skips_implementation_when_technical_analysis_requires_no_delivery(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("discovery", "idea", "Без реализации", status="investment_decision")
    ticket.implementation_required = False
    store.save(ticket)

    html = render_board(store, load_all_workflows(), "discovery")

    assert 'name="target" value="ready_for_validation"' in html
    assert "Переместить → Готово к валидации" in html


def test_ui_closes_confirmed_correction_instead_of_starting_analysis(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    store.create("discovery", "correction", "Уточнить требования", status="human")

    html = render_board(store, load_all_workflows(), "discovery")

    assert 'name="target" value="done"' in html
    assert "Переместить → Готово" in html
