from __future__ import annotations

import argparse
import asyncio
import logging
import json
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from .config import load_all_workflows
from .budget_ledger import BudgetLedger
from .control import DeliverySessionStore, SessionError, WorkerControl
from .git_trees import GitTreeManager
from .orchestrator import Orchestrator, resume_rework
from .tickets import TicketStore
from .framework_ui import serve, start_server


def default_cli_authorizer(**_: Any) -> tuple[bool, str | None]:
    """Deny budget mutations until the CLI is wired to an explicit policy."""
    return False, None


def project_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise argparse.ArgumentTypeError(f"Путь не существует: {path}")
    return path


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("Значение не может быть отрицательным")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe", description="Pull-оркестратор тикетов Codex")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Инициализировать хранилище тикетов .vibe в проекте"); init.add_argument("project", type=project_path)
    add = sub.add_parser("add", help="Создать тикет"); add.add_argument("project", type=project_path); add.add_argument("process", choices=["discovery", "delivery", "process_management"]); add.add_argument("type"); add.add_argument("title"); add.add_argument("--description", default=""); add.add_argument("--priority", type=int, default=100); add.add_argument("--parent"); add.add_argument("--status")
    ls = sub.add_parser("list", help="Показать список тикетов"); ls.add_argument("project", type=project_path); ls.add_argument("--process", choices=["discovery", "delivery", "process_management"])
    move = sub.add_parser("move", help="Переместить тикет вручную"); move.add_argument("project", type=project_path); move.add_argument("ticket"); move.add_argument("status")
    run = sub.add_parser("run", help="Запустить оркестратор"); run.add_argument("project", type=project_path); run.add_argument("--poll", type=float, default=2.0); run.add_argument("--max-agents", type=non_negative_int); run.add_argument("--ui", action="store_true", help="Запустить UI вместе с оркестратором"); run.add_argument("--ui-host", default="127.0.0.1"); run.add_argument("--ui-port", type=int, default=8765); run.add_argument("--no-browser", action="store_true")
    workers = sub.add_parser("workers", help="Показать или изменить лимит воркеров"); workers.add_argument("project", type=project_path); workers.add_argument("count", nargs="?", type=non_negative_int)
    release_retry = sub.add_parser("release-retry", help="Повторить интеграцию дерева тикета"); release_retry.add_argument("project", type=project_path); release_retry.add_argument("ticket")
    resume = sub.add_parser("resume-rework", help="Разрешить дополнительный проход реворка"); resume.add_argument("project", type=project_path); resume.add_argument("ticket")
    ui = sub.add_parser("ui", help="Запустить минимальный локальный Kanban UI"); ui.add_argument("project", type=project_path); ui.add_argument("--host", default="127.0.0.1"); ui.add_argument("--port", type=int, default=8765); ui.add_argument("--no-browser", action="store_true")
    session = sub.add_parser("session", aliases=["sessions"], help="Управление Delivery-сессиями")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    create_session = session_sub.add_parser("create", help="Создать черновик сессии"); create_session.add_argument("project", type=project_path); create_session.add_argument("title", nargs="?", default=""); create_session.add_argument("--title", dest="title_option")
    list_sessions = session_sub.add_parser("list", help="Показать сессии"); list_sessions.add_argument("project", type=project_path)
    show_session = session_sub.add_parser("show", help="Показать сессию"); show_session.add_argument("project", type=project_path); show_session.add_argument("session")
    for action, help_text in (("add", "Добавить тикет в черновик"), ("remove", "Убрать тикет из черновика")):
        command = session_sub.add_parser(action, help=help_text); command.add_argument("project", type=project_path); command.add_argument("session"); command.add_argument("ticket")
    activate_session = session_sub.add_parser("activate", help="Активировать сессию"); activate_session.add_argument("project", type=project_path); activate_session.add_argument("session")
    for action, help_text in (("complete", "Завершить сессию"), ("cancel", "Отменить сессию")):
        command = session_sub.add_parser(action, help=help_text); command.add_argument("project", type=project_path); command.add_argument("session"); command.add_argument("--override", "--override-reason", "--reason", dest="override_reason")
    budget = sub.add_parser("budget", help="Ручные budget decisions и audit trail")
    budget_sub = budget.add_subparsers(dest="budget_command", required=True)
    def common(command):
        command.add_argument("project", type=project_path); command.add_argument("--actor", required=True); command.add_argument("--reason", required=True); command.add_argument("--reference", required=True); command.add_argument("--expires-at"); command.add_argument("--one-shot", action="store_true"); command.add_argument("--decision-id")
    increase = budget_sub.add_parser("increase-limit", help="Увеличить лимит для будущего admission"); common(increase); increase.add_argument("--scope", dest="target_scope", choices=["ticket", "session"], required=True); increase.add_argument("--target", dest="target_id", required=True); increase.add_argument("--dimension", choices=["tokens", "points", "runs"], required=True); increase.add_argument("--delta", type=non_negative_int, required=True)
    overrun = budget_sub.add_parser("allow-overrun", help="Разрешить overrun только указанному target"); common(overrun); overrun.add_argument("--scope", dest="target_scope", choices=["ticket", "session", "run"], required=True); overrun.add_argument("--target", dest="target_id", required=True); overrun.add_argument("--dimension", choices=["tokens", "points", "runs"], action="append", required=True)
    unknown = budget_sub.add_parser("resolve-unknown", help="Разрешить unknown run с evidence или estimate"); common(unknown); unknown.add_argument("run_id"); unknown.add_argument("--evidence", help="JSON evidence payload"); unknown.add_argument("--estimate", help="JSON accepted estimate payload"); unknown.add_argument("--confidence", type=float)
    decisions = budget_sub.add_parser("decisions", help="Прочитать append-only audit trail"); decisions.add_argument("project", type=project_path); decisions.add_argument("--operation"); decisions.add_argument("--scope", dest="target_scope"); decisions.add_argument("--target", dest="target_id")
    return parser


def main(argv: Sequence[str] | None = None, *, authorizer: Callable[..., Any] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "budget":
        # Actor identity is audit data, not an authorization decision. An
        # embedding application must inject its policy explicitly; standalone
        # CLI invocations remain default-deny.
        ledger = BudgetLedger(args.project, authorizer=authorizer or default_cli_authorizer)
        try:
            if args.budget_command == "increase-limit": result = ledger.increase_limit(actor=args.actor, target_scope=args.target_scope, target_id=args.target_id, dimension=args.dimension, delta=args.delta, reason=args.reason, reference=args.reference, expires_at=args.expires_at, one_shot=args.one_shot, decision_id=args.decision_id)
            elif args.budget_command == "allow-overrun": result = ledger.allow_overrun(actor=args.actor, target_scope=args.target_scope, target_id=args.target_id, dimensions=args.dimension, reason=args.reason, reference=args.reference, expires_at=args.expires_at, one_shot=args.one_shot, decision_id=args.decision_id)
            elif args.budget_command == "resolve-unknown":
                result = ledger.resolve_unknown(actor=args.actor, run_id=args.run_id, reason=args.reason, reference=args.reference, evidence=json.loads(args.evidence) if args.evidence else None, estimate=json.loads(args.estimate) if args.estimate else None, confidence=args.confidence, expires_at=args.expires_at, one_shot=args.one_shot, decision_id=args.decision_id)
            else: result = ledger.list_decisions(operation=args.operation, target_scope=args.target_scope, target_id=args.target_id)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True)); return
        except (KeyError, PermissionError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise SystemExit(str(exc)) from exc
    if args.command == "init":
        store = TicketStore(args.project); store.init(); print(f"Инициализировано: {store.root}"); return
    if args.command in {"session", "sessions"}:
        store = TicketStore(args.project); store.init(); sessions = DeliverySessionStore(args.project)
        try:
            if args.session_command == "create":
                title = args.title_option if args.title_option is not None else args.title
                print(sessions.create(title).id); return
            if args.session_command == "list":
                for item in sessions.list(): print(f"{item.id:12} {item.status:10} {len(item.participants):3} {item.title}")
                return
            if args.session_command == "show":
                print(yaml.safe_dump(sessions.get(args.session).to_dict(), sort_keys=False, allow_unicode=True), end=""); return
            if args.session_command == "add":
                print(f"{sessions.add(args.session, args.ticket, store).id}: добавлен {args.ticket}"); return
            if args.session_command == "remove":
                print(f"{sessions.remove(args.session, args.ticket).id}: удален {args.ticket}"); return
            if args.session_command == "activate":
                print(f"{sessions.activate(args.session, store).id}: active"); return
            if args.session_command == "complete":
                print(f"{sessions.complete(args.session, store, args.override_reason).id}: completed"); return
            if args.session_command == "cancel":
                print(f"{sessions.cancel(args.session, store, args.override_reason).id}: cancelled"); return
        except SessionError as exc:
            raise SystemExit(str(exc)) from exc
    if args.command == "add":
        store = TicketStore(args.project); store.init(); ticket = store.create(args.process, args.type, args.title, description=args.description, priority=args.priority, parent=args.parent, status=args.status); print(ticket.id); return
    if args.command == "list":
        store = TicketStore(args.project); store.init()
        for ticket in store.list(args.process): print(f"{ticket.id:12} {ticket.process:18} {ticket.status:28} {ticket.type:12} {ticket.title}")
        return
    if args.command == "move":
        store = TicketStore(args.project); workflows = load_all_workflows(); ticket = store.get(args.ticket)
        if args.status not in workflows[ticket.process].by_id: raise SystemExit(f"Неизвестный статус {args.status!r} для процесса {ticket.process}")
        ticket.status = args.status; store.save(ticket); print(f"{ticket.id} -> {ticket.status}"); return
    if args.command == "workers":
        control = WorkerControl(args.project)
        if args.count is not None: control.set_limit(args.count)
        print(f"Лимит воркеров: {control.get_limit()}"); return
    if args.command == "release-retry":
        store = TicketStore(args.project); store.init(); manager = GitTreeManager(args.project, store)
        if not manager.reset_integration(args.ticket): raise SystemExit("Для тикета нет ожидающего merge-конфликта")
        ticket = store.get(args.ticket); ticket.last_outcome = None; ticket.last_summary = None; store.save(ticket)
        print(f"Повтор интеграции разрешен: {args.ticket}"); return
    if args.command == "resume-rework":
        store = TicketStore(args.project); store.init()
        try:
            ticket = resume_rework(store, args.ticket)
        except (KeyError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Дополнительный проход реворка разрешен: {ticket.id}"); return
    if args.command == "run":
        server = None
        try:
            if args.ui:
                server, _ = start_server(args.project, args.ui_host, args.ui_port, not args.no_browser)
            asyncio.run(Orchestrator(args.project, args.poll, args.max_agents).run_forever())
        finally:
            if server is not None:
                server.shutdown(); server.server_close()
        return
    if args.command == "ui": serve(args.project, args.host, args.port, not args.no_browser); return
