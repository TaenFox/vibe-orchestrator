from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .config import load_all_workflows
from .control import WorkerControl
from .git_trees import GitTreeManager
from .orchestrator import Orchestrator
from .tickets import TicketStore
from .ui import serve


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
    run = sub.add_parser("run", help="Запустить оркестратор"); run.add_argument("project", type=project_path); run.add_argument("--poll", type=float, default=2.0); run.add_argument("--max-agents", type=non_negative_int)
    workers = sub.add_parser("workers", help="Показать или изменить лимит воркеров"); workers.add_argument("project", type=project_path); workers.add_argument("count", nargs="?", type=non_negative_int)
    release_retry = sub.add_parser("release-retry", help="Повторить интеграцию дерева тикета"); release_retry.add_argument("project", type=project_path); release_retry.add_argument("ticket")
    ui = sub.add_parser("ui", help="Запустить минимальный локальный Kanban UI"); ui.add_argument("project", type=project_path); ui.add_argument("--host", default="127.0.0.1"); ui.add_argument("--port", type=int, default=8765); ui.add_argument("--no-browser", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "init":
        store = TicketStore(args.project); store.init(); print(f"Инициализировано: {store.root}"); return
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
    if args.command == "run": asyncio.run(Orchestrator(args.project, args.poll, args.max_agents).run_forever()); return
    if args.command == "ui": serve(args.project, args.host, args.port, not args.no_browser); return
