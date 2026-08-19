from __future__ import annotations

import html
import json
import logging
import urllib.parse
import webbrowser
from pathlib import Path
from threading import Thread
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .config import load_all_workflows
from .control_db import ControlPlaneReader
from .control import WorkerControl
from .tickets import TicketStore, next_status_for_ticket

LOG = logging.getLogger(__name__)

STYLE = """
:root { color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif; background: #10151b; color: #edf2f7; }
* { box-sizing: border-box; } body { margin: 0; } a { color: #8bd5ff; text-decoration: none; }
header { position: sticky; top: 0; z-index: 2; display: flex; gap: 18px; align-items: center; padding: 16px 24px; background: #151c24ee; backdrop-filter: blur(12px); border-bottom: 1px solid #293544; }
header h1 { margin: 0; font-size: 18px; letter-spacing: .04em; } nav { display: flex; gap: 8px; } nav a, button { border: 1px solid #344454; border-radius: 8px; padding: 8px 11px; background: #1c2732; color: #edf2f7; cursor: pointer; } nav a.active { background: #2b6f8f; border-color: #65c8f1; }
main { max-width: 1500px; margin: 0 auto; padding: 24px; } .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 20px; }
input, select, textarea { width: 100%; border: 1px solid #344454; border-radius: 8px; padding: 9px; background: #18212b; color: inherit; font: inherit; } .toolbar input { width: min(360px, 100%); }
.board { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; align-items: start; } .column { min-height: 130px; padding: 12px; background: #151c24; border: 1px solid #293544; border-radius: 12px; } .column h2 { margin: 0 0 12px; font-size: 13px; color: #94a7b8; text-transform: uppercase; letter-spacing: .08em; }
.card { display: block; margin: 9px 0; padding: 12px; background: #1b2631; border: 1px solid #334353; border-radius: 9px; } .card:hover { border-color: #65c8f1; transform: translateY(-1px); } .card strong { display: block; margin: 6px 0; overflow-wrap: anywhere; } .meta { color: #94a7b8; font-size: 12px; } .summary { margin-top: 8px; color: #c6d0da; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
.panel { max-width: 900px; padding: 24px; background: #151c24; border: 1px solid #293544; border-radius: 14px; } .panel h2 { margin-top: 0; overflow-wrap: anywhere; } .field { margin: 16px 0; } .field label { display: block; margin-bottom: 6px; color: #94a7b8; font-size: 12px; text-transform: uppercase; } .actions { display: flex; flex-wrap: wrap; gap: 8px; } .empty { color: #94a7b8; }
@media (max-width: 650px) { header, main { padding: 14px; } .board { grid-template-columns: 1fr; } }
"""


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _parse_body(request_body: bytes) -> dict[str, str]:
    values = urllib.parse.parse_qs(request_body.decode("utf-8"), keep_blank_values=True)
    return {key: items[-1] for key, items in values.items()}


def _layout(content: str, process: str = "discovery") -> str:
    nav = "".join(f'<a class="{"active" if item == process else ""}" href="/?process={item}">{item}</a>'
                   for item in ("discovery", "delivery", "process_management"))
    return f"<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Vibe Control</title><style>{STYLE}</style></head><body><header><h1>VIBE CONTROL</h1><nav>{nav}</nav><button type=button onclick='location.reload()'>Обновить</button></header>{content}<script>setInterval(() => {{ if (!document.hidden && !document.querySelector(':focus')) location.reload(); }}, 15000);</script></body></html>"


def _ticket_data(ticket: Any) -> dict[str, Any]:
    if hasattr(ticket, "to_dict"):
        return ticket.to_dict()
    payload = ticket.get("payload", {}) if isinstance(ticket, dict) else {}
    data = {**payload, **ticket}
    data["id"] = ticket.get("ticket_id", data.get("id"))
    data["type"] = ticket.get("ticket_type", data.get("type"))
    return data


def _board_html(store: TicketStore, workflows: dict, process: str, search: str = "", reader: ControlPlaneReader | None = None) -> str:
    tickets = [_ticket_data(ticket) for ticket in (reader.list_tickets(process=process, limit=1_000_000) if reader else store.list(process))]
    tickets = [ticket for ticket in tickets if not search or search.lower() in f"{ticket.get('id', ticket.get('ticket_id', ''))} {ticket.get('title', '')} {ticket.get('description', '')}".lower()]
    workflow = workflows[process]
    columns = []
    for stage in workflow.stages:
        cards = []
        for ticket in tickets:
            if ticket.get("status") != stage.id:
                continue
            ticket_id = ticket.get("id", ticket.get("ticket_id"))
            cards.append(f'<a class="card" href="/ticket/{_escape(ticket_id)}"><span class=meta>{_escape(ticket_id)} · приоритет {_escape(ticket.get("priority", 100))}</span><strong>{_escape(ticket.get("title", ""))}</strong><div class=summary>{_escape(ticket.get("last_summary") or ticket.get("description") or "")}</div></a>')
        columns.append(f'<section class=column><h2>{_escape(stage.title)} <span class=meta>({len(cards)})</span></h2>{"".join(cards) or "<div class=empty>Пусто</div>"}</section>')
    content = f'<main><div class=toolbar><form method=get><input name=search value="{_escape(search)}" placeholder="Поиск по тикетам"><input type=hidden name=process value="{_escape(process)}"><button>Найти</button></form><a href="/new?process={_escape(process)}">Создать тикет</a></div><div class=board>{"".join(columns)}</div></main>'
    return _layout(content, process)


def _ticket_html(store: TicketStore, workflows: dict, ticket_id: str, reader: ControlPlaneReader | None = None) -> str:
    ticket = store.get(ticket_id)
    data = _ticket_data(reader.get_ticket(ticket_id)) if reader else _ticket_data(ticket)
    data = data or _ticket_data(ticket)
    next_status = next_status_for_ticket(store, ticket)
    action = f'<form method=post action="/ticket/{_escape(ticket.id)}/move"><input type=hidden name=target value="{_escape(next_status)}"><button>Перевести в {_escape(next_status)}</button></form>' if next_status else ""
    history = "".join(f'<li><b>{_escape(item.get("event", "event"))}</b> · {_escape(item.get("stage", ""))} · {_escape(item.get("timestamp", ""))}<br>{_escape(item.get("summary", ""))}</li>' for item in reversed(data.get("run_history", [])))
    process = data.get("process", ticket.process)
    content = f'<main><article class=panel><a href="/?process={_escape(process)}">← К доске</a><h2>{_escape(data.get("title", ""))}</h2><div class=meta>{_escape(data.get("id", ticket.id))} · {_escape(data.get("type", ""))} · {_escape(data.get("status", ""))} · приоритет {_escape(data.get("priority", 100))}</div><div class=field><label>Описание</label><div class=summary>{_escape(data.get("description") or "(пусто)")}</div></div><div class=field><label>Последний результат</label><div class=summary>{_escape(data.get("last_summary") or "нет")}</div></div><div class=actions>{action}</div><div class=field><label>История запусков</label><ol>{history or "<li class=empty>Запусков пока нет</li>"}</ol></div></article></main>'
    return _layout(content, process)


def _new_html(process: str) -> str:
    content = f'<main><article class=panel><a href="/?process={_escape(process)}">← К доске</a><h2>Новый тикет</h2><form method=post action=/tickets><input type=hidden name=process value="{_escape(process)}"><div class=field><label>Тип</label><input name=type required value="idea"></div><div class=field><label>Заголовок</label><input name=title required autofocus></div><div class=field><label>Описание</label><textarea name=description rows=8></textarea></div><div class=field><label>Приоритет</label><input name=priority type=number min=0 value=100></div><div class=actions><button type=submit>Создать</button></div></form></article></main>'
    return _layout(content, process)


def create_app(project: str | Path) -> Starlette:
    root = Path(project).resolve()
    store = TicketStore(root)
    store.init()
    workflows = load_all_workflows()
    workers = WorkerControl(root)
    reader = ControlPlaneReader(root) if (root / ".vibe" / "control.sqlite3").exists() else None

    async def board(request):
        process = request.query_params.get("process", "discovery")
        if process not in workflows:
            return Response("Unknown process", status_code=404)
        return HTMLResponse(_board_html(store, workflows, process, request.query_params.get("search", ""), reader))

    async def ticket(request):
        try:
            return HTMLResponse(_ticket_html(store, workflows, request.path_params["ticket_id"], reader))
        except KeyError:
            return Response("Ticket not found", status_code=404)

    async def new_ticket(request):
        return HTMLResponse(_new_html(request.query_params.get("process", "discovery")))

    async def create_ticket(request):
        data = _parse_body(await request.body())
        try:
            ticket = store.create(data["process"], data.get("type", "idea"), data["title"], description=data.get("description", ""), priority=int(data.get("priority", "100")))
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/?process={urllib.parse.quote(ticket.process)}", status_code=303)

    async def move_ticket(request):
        try:
            ticket = store.get(request.path_params["ticket_id"])
            target = _parse_body(await request.body()).get("target")
            if target != next_status_for_ticket(store, ticket):
                return Response("Transition unavailable", status_code=400)
            ticket.status = target
            store.save(ticket)
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/?process={ticket.process}", status_code=303)

    async def workers_page(request):
        if request.method == "GET":
            return JSONResponse({"limit": workers.get_limit()})
        try:
            data = _parse_body(await request.body())
            workers.set_limit(int(data["count"]))
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return JSONResponse({"limit": workers.get_limit()})

    async def health(request):
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[
        Route("/", board), Route("/healthz", health), Route("/ticket/{ticket_id}", ticket),
        Route("/new", new_ticket), Route("/tickets", create_ticket, methods=["POST"]),
        Route("/ticket/{ticket_id}/move", move_ticket, methods=["POST"]),
        Route("/workers", workers_page, methods=["GET", "POST"]),
    ])


class FrameworkServer:
    def __init__(self, server: uvicorn.Server, thread: Thread):
        self.server = server
        self.thread = thread

    def shutdown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def server_close(self) -> None:
        return None


def start_server(project: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> tuple[FrameworkServer, Thread]:
    app = create_app(project)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = Thread(target=server.run, name="vibe-ui", daemon=True)
    thread.start()
    url = f"http://{host}:{port}"
    print(f"Интерфейс: {url}")
    if open_browser:
        webbrowser.open(url)
    return FrameworkServer(server, thread), thread


def serve(project: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    app = create_app(project)
    url = f"http://{host}:{port}"
    print(f"Интерфейс: {url}")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port, log_level="warning")
