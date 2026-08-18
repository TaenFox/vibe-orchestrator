from pathlib import Path

from vibe_orchestrator.cli import build_parser
from vibe_orchestrator.config import load_all_workflows
from vibe_orchestrator.control import DeliverySessionStore, WorkerControl
from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.ui import AUTO_REFRESH_SECONDS, render_board


def test_cli_help_is_localized():
    help_text = build_parser().format_help()
    assert "Pull-оркестратор тикетов Codex" in help_text
    assert "Создать тикет" in help_text
    assert "Запустить оркестратор" in help_text
    assert "изменить лимит воркеров" in help_text


def test_cli_accepts_zero_worker_limit(tmp_path: Path):
    run_args = build_parser().parse_args(["run", str(tmp_path), "--max-agents", "0"])
    workers_args = build_parser().parse_args(["workers", str(tmp_path), "0"])

    assert run_args.max_agents == 0
    assert workers_args.count == 0


def test_workflow_titles_are_localized():
    workflows = load_all_workflows()

    assert workflows["delivery"].title == "Доставка"
    assert workflows["delivery"].by_id["ready_for_review"].title == "Готово к ревью"
    assert workflows["process_management"].by_id["auto_acceptance"].title == "Авто-приемка"


def test_generated_vibe_readme_is_localized(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()

    readme = (tmp_path / ".vibe" / "README.md").read_text(encoding="utf-8")
    gitignore = (tmp_path / ".vibe" / ".gitignore").read_text(encoding="utf-8")
    assert "Состояние тикетов для vibe-orchestrator" in readme
    assert "`runs/` содержит локальные артефакты запусков" in readme
    assert "`run.json`, `events.jsonl`, `result.json`" in readme
    assert "tmp/" in gitignore
    assert "tickets/" in gitignore
    assert "sessions/" in gitignore
    assert "sessions.lock" in gitignore


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
    assert "setInterval(refresh" in html
    assert "document.querySelector('details[open]')" in html
    assert "document.activeElement?.matches('input, select, textarea')" in html
    assert f"частичное автообновление {AUTO_REFRESH_SECONDS}с" in html
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


def test_ui_shows_retry_state_and_manual_retry_action(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    pending = store.create("delivery", "task", "Temporary failure", status="review")
    pending.last_outcome = "failed"
    pending.consecutive_failures = 1
    pending.retry_after = "2026-01-01T00:00:05+00:00"
    store.save(pending)
    exhausted = store.create("delivery", "task", "Permanent failure", status="acceptance")
    exhausted.last_outcome = "failed"
    exhausted.consecutive_failures = 3
    store.save(exhausted)

    html = render_board(store, load_all_workflows(), "delivery")

    assert "ожидает автоповтора" in html
    assert "Ошибок подряд" in html
    assert "2026-01-01T00:00:05+00:00" in html
    assert '<form method="post" action="/retry">' in html
    assert "Повторить" in html


def test_ui_offers_release_retry_after_merge_conflict(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Conflict", status="ready_for_release")
    ticket.last_outcome = "integration_conflict"
    ticket.last_summary = "Конфликт в app.py"
    store.save(ticket)

    html = render_board(store, load_all_workflows(), "delivery")

    assert "Повторить интеграцию" in html


def test_ui_controls_worker_limit_including_zero(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    ticket = store.create("delivery", "task", "Running", status="review")
    ticket.active_run = "run-1"
    store.save(ticket)
    control = WorkerControl(tmp_path)
    control.set_limit(0)

    html = render_board(store, load_all_workflows(), "delivery", control)

    assert '<form method="post" action="/workers">' in html
    assert 'name="count" min="0" value="0"' in html
    assert "активно 1" in html


def test_ui_shows_delivery_sessions_membership_and_aggregates(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.init()
    mandatory = store.create("delivery", "story", "Обязательная")
    optional = store.create("delivery", "task", "Необязательная", status="review", mandatory=False)
    optional.blocked_by = ["DEL-BLOCK"]
    optional.active_run = "run-1"
    store.save(optional)
    sessions = DeliverySessionStore(tmp_path)
    session = sessions.create("Релиз 1")
    sessions.add(session.id, mandatory.id, store)
    sessions.add(session.id, optional.id, store)
    mandatory.status = "done"
    store.save(mandatory)

    html = render_board(store, load_all_workflows(), "delivery", session_store=sessions)

    assert "Delivery-сессии" in html
    assert "Релиз 1" in html
    assert "mandatory 1 · optional 1 · done 1 · blocked 1 · active_run 1" in html
    assert f"сессия: {session.id}" in html
    assert "Необязательная" in html
    assert 'action="/session/activate"' in html
    assert 'action="/session/remove"' in html
