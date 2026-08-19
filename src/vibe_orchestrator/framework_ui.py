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
.view-switch { display: inline-flex; gap: 3px; padding: 3px; background: #121920; border: 1px solid #293544; border-radius: 9px; } .view-switch a { padding: 7px 10px; border-radius: 6px; color: #94a7b8; } .view-switch a.active { background: #2b6f8f; color: #edf2f7; }
.ticket-table { width: 100%; border-collapse: separate; border-spacing: 0 6px; } .ticket-table th { padding: 0 12px 6px; color: #94a7b8; font-size: 11px; font-weight: 500; text-align: left; text-transform: uppercase; letter-spacing: .07em; } .ticket-row { cursor: grab; } .ticket-row.dragging { opacity: .45; } .ticket-row td { padding: 11px 12px; background: #151c24; border-top: 1px solid #293544; border-bottom: 1px solid #293544; vertical-align: middle; } .ticket-row td:first-child { border-left: 1px solid #293544; border-radius: 9px 0 0 9px; } .ticket-row td:last-child { border-right: 1px solid #293544; border-radius: 0 9px 9px 0; } .ticket-row:hover td { border-color: #65c8f1; background: #1b2a36; } .ticket-row.agent-active td { background: #1c342f; border-color: #4dbb8d; } .ticket-row.needs-attention td { background: #3a3020; border-color: #c49a4a; } .ticket-title { display: block; color: #edf2f7; font-weight: 650; overflow-wrap: anywhere; } .ticket-id { color: #94a7b8; font-size: 12px; } .status-label { white-space: nowrap; color: #c6d0da; font-size: 12px; } .progress { display: flex; gap: 3px; min-width: 150px; } .progress-step { width: 18px; height: 6px; border-radius: 3px; background: #344454; } .progress-step.done { background: #58a6c9; } .progress-step.current { background: #f0c674; box-shadow: 0 0 0 2px #f0c67433; } .progress-step.final { background: #4dbb8d; } .agent-badge, .attention-badge { display: inline-block; margin-left: 6px; padding: 2px 5px; border-radius: 999px; font-size: 10px; white-space: nowrap; } .agent-badge { color: #b8f1d7; background: #28634d; } .attention-badge { color: #ffe2a0; background: #765724; } .meta { color: #94a7b8; font-size: 12px; } .summary { margin-top: 8px; color: #c6d0da; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
.panel { max-width: 900px; padding: 24px; background: #151c24; border: 1px solid #293544; border-radius: 14px; } .panel h2 { margin-top: 0; overflow-wrap: anywhere; } .field { margin: 16px 0; } .field label { display: block; margin-bottom: 6px; color: #94a7b8; font-size: 12px; text-transform: uppercase; } .actions { display: flex; flex-wrap: wrap; gap: 8px; } .empty { color: #94a7b8; }
@media (max-width: 800px) { .ticket-table, .ticket-table tbody, .ticket-table tr, .ticket-table td { display: block; } .ticket-table thead { display: none; } .ticket-row { margin: 10px 0; } .ticket-row td { border-left: 1px solid #293544; border-right: 1px solid #293544; border-radius: 0; } .ticket-row td:first-child { border-radius: 9px 9px 0 0; } .ticket-row td:last-child { border-radius: 0 0 9px 9px; } }
"""


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _parse_body(request_body: bytes) -> dict[str, str]:
    values = urllib.parse.parse_qs(request_body.decode("utf-8"), keep_blank_values=True)
    return {key: items[-1] for key, items in values.items()}


def _layout(content: str, process: str = "discovery") -> str:
    nav = "".join(f'<a class="{"active" if item == process else ""}" href="/?process={item}">{item}</a>'
                   for item in ("discovery", "delivery", "process_management"))
    script = """
    const rows = [...document.querySelectorAll('tr[data-ticket][draggable=true]')]; let dragged = null;
    rows.forEach(row => {
      row.addEventListener('dragstart', () => { dragged = row; row.classList.add('dragging'); });
      row.addEventListener('dragend', () => { row.classList.remove('dragging'); dragged = null; });
      row.addEventListener('dragover', event => { if (dragged && dragged.dataset.status === row.dataset.status) event.preventDefault(); });
      row.addEventListener('drop', async event => {
        event.preventDefault(); if (!dragged || dragged === row || dragged.dataset.status !== row.dataset.status) return;
        const tbody = row.parentElement; const all = [...tbody.querySelectorAll(`tr[data-status="${CSS.escape(row.dataset.status)}"]`)].filter(item => item !== dragged);
        const target = all.indexOf(row); all.splice(target < 0 ? all.length : target, 0, dragged); all.forEach(item => tbody.appendChild(item));
        const ticketIds = all.map(item => item.dataset.ticket);
        const response = await fetch('/tickets/reorder', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ticket_ids: ticketIds, process: document.body.dataset.process || ''})});
        if (!response.ok) location.reload(); else location.reload();
      });
    });
    setInterval(() => { if (!document.hidden && !document.querySelector(':focus')) location.reload(); }, 15000);
    """
    return f"<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Vibe Control</title><style>{STYLE}</style></head><body data-process='{_escape(process)}'><header><h1>VIBE CONTROL</h1><nav>{nav}</nav><button type=button onclick='location.reload()'>Обновить</button></header>{content}<script>{script}</script></body></html>"


def _ticket_data(ticket: Any) -> dict[str, Any]:
    if hasattr(ticket, "to_dict"):
        return ticket.to_dict()
    payload = ticket.get("payload", {}) if isinstance(ticket, dict) else {}
    data = {**payload, **ticket}
    data["id"] = ticket.get("ticket_id", data.get("id"))
    data["type"] = ticket.get("ticket_type", data.get("type"))
    return data


def _board_html(store: TicketStore, workflows: dict, process: str, search: str = "", reader: ControlPlaneReader | None = None,
                worker_limit: int | None = None, view: str = "wip") -> str:
    source = reader.list_tickets(process=process, limit=1_000_000) if reader else store.list(process)
    tickets = [_ticket_data(ticket) for ticket in source]
    tickets = [ticket for ticket in tickets if not search or search.lower() in f"{ticket.get('id', ticket.get('ticket_id', ''))} {ticket.get('title', '')} {ticket.get('description', '')}".lower()]
    workflow = workflows[process]
    final_stage = workflow.stages[-1]
    positions = {stage.id: index for index, stage in enumerate(workflow.stages)}
    if view == "done":
        tickets = [ticket for ticket in tickets if ticket.get("status") == final_stage.id]
        tickets.sort(key=lambda ticket: (ticket.get("updated_at") or "", ticket.get("id", "")), reverse=True)
    else:
        tickets = [ticket for ticket in tickets if ticket.get("status") != final_stage.id]
        tickets.sort(key=lambda ticket: (-positions.get(ticket.get("status"), -1), ticket.get("priority", 100), ticket.get("updated_at") or "", ticket.get("id", "")))

    rows = []
    for ticket in tickets:
        ticket_id = ticket.get("id", ticket.get("ticket_id"))
        status = ticket.get("status", "")
        current_position = positions.get(status, 0)
        progress = "".join(f'<span class="progress-step {"final" if index == len(workflow.stages) - 1 else "done" if index < current_position else "current" if index == current_position else ""}" title="{_escape(stage.title)}"></span>' for index, stage in enumerate(workflow.stages))
        active = bool(ticket.get("active_run"))
        if reader and ticket.get("active_run"):
            run = reader.get_run(ticket["active_run"])
            active = bool(run and run.get("state") == "started")
        badge = '<span class=agent-badge>агент работает</span>' if active else ""
        stage = workflow.by_id.get(status)
        attention_reason = ""
        if stage and stage.kind == "human":
            attention_reason = "нужно ваше действие"
        elif ticket.get("blocked_reason"):
            attention_reason = str(ticket.get("blocked_reason"))
        attention = f'<span class=attention-badge title="{_escape(attention_reason)}">внимание</span>' if attention_reason else ""
        parent = ticket.get("parent") or "—"
        rows.append(f'<tr class="ticket-row{" agent-active" if active else ""}{" needs-attention" if attention_reason else ""}" data-ticket="{_escape(ticket_id)}" data-status="{_escape(status)}" draggable="{"true" if view == "wip" else "false"}"><td><a href="/ticket/{_escape(ticket_id)}"><span class=ticket-title>{_escape(ticket.get("title", ""))}{badge}{attention}</span><span class=ticket-id>{_escape(ticket_id)} · {_escape(ticket.get("type", ticket.get("ticket_type", "")))}</span></a></td><td><div class=progress aria-label="Прогресс по статусам">{progress}</div><span class=status-label>{_escape(stage.title if stage else status)}</span></td><td class=meta>{_escape(parent)}</td><td class=meta>{_escape(str(ticket.get("updated_at", "")).replace("T", " ")[:16])}</td></tr>')
    switch = f'<div class=view-switch><a class="{"active" if view == "wip" else ""}" href="/?process={_escape(process)}&view=wip&search={urllib.parse.quote(search)}">WIP</a><a class="{"active" if view == "done" else ""}" href="/?process={_escape(process)}&view=done&search={urllib.parse.quote(search)}">Done</a></div>'
    worker_form = f'<form method=post action=/workers><input type=hidden name=process value="{_escape(process)}"><button name=delta value=-1 aria-label="Уменьшить количество воркеров">−1</button><span class=meta>Воркеры: <strong>{_escape(worker_limit if worker_limit is not None else "?")}</strong></span><button name=delta value=1 aria-label="Увеличить количество воркеров">+1</button></form>' if worker_limit is not None else ""
    table = f'<table class=ticket-table><thead><tr><th>Тикет</th><th>Прогресс</th><th>Родитель</th><th>Обновлён</th></tr></thead><tbody>{"".join(rows)}</tbody></table>' if rows else '<div class=empty>В этом представлении тикетов нет</div>'
    content = f'<main><div class=toolbar><form method=get><input name=search value="{_escape(search)}" placeholder="Поиск по тикетам"><input type=hidden name=process value="{_escape(process)}"><input type=hidden name=view value="{_escape(view)}"><button>Найти</button></form>{switch}<a href="/new?process={_escape(process)}">Создать тикет</a>{worker_form}</div>{table}</main>'
    return _layout(content, process)


def _ticket_html(store: TicketStore, workflows: dict, ticket_id: str, reader: ControlPlaneReader | None = None) -> str:
    ticket = store.get(ticket_id)
    data = _ticket_data(reader.get_ticket(ticket_id)) if reader else _ticket_data(ticket)
    data = data or _ticket_data(ticket)
    next_status = next_status_for_ticket(store, ticket)
    action = f'<form method=post action="/ticket/{_escape(ticket.id)}/move"><input type=hidden name=target value="{_escape(next_status)}"><button>Перевести в {_escape(next_status)}</button></form>' if next_status else ""
    history = "".join(f'<li><b>{_escape(item.get("event", "event"))}</b> · {_escape(item.get("stage", ""))} · {_escape(item.get("timestamp", ""))}<br>{_escape(item.get("summary", ""))}</li>' for item in reversed(data.get("run_history", [])))
    run_cards = []
    if reader:
        for run in reader.list_runs(ticket_id=ticket_id, limit=100):
            result = reader.list_results(run_id=run["run_id"])
            usage = reader.get_token_usage(run["run_id"])
            result_data = result[0] if result else {}
            usage_data = usage[0] if usage else {}
            total_tokens = usage_data.get("total_tokens")
            token_label = f" · {total_tokens:,} токенов".replace(",", " ") if total_tokens is not None else " · токены неизвестны"
            run_cards.append(f'<li><b>{_escape(run.get("stage") or "run")}</b> · {_escape(run.get("state") or "unknown")}{_escape(token_label)}<br><span class=meta>{_escape(run.get("run_id"))}</span><br>{_escape(result_data.get("summary") or "Результат пока отсутствует")}</li>')
    run_section = f'<div class=field><label>Запуски</label><ol>{"".join(run_cards) or "<li class=empty>Запусков пока нет</li>"}</ol></div>'
    process = data.get("process", ticket.process)
    content = f'<main><article class=panel><a href="/?process={_escape(process)}">← К доске</a><h2>{_escape(data.get("title", ""))}</h2><div class=meta>{_escape(data.get("id", ticket.id))} · {_escape(data.get("type", ""))} · {_escape(data.get("status", ""))} · приоритет {_escape(data.get("priority", 100))}</div><div class=field><label>Описание</label><div class=summary>{_escape(data.get("description") or "(пусто)")}</div></div><div class=field><label>Последний результат</label><div class=summary>{_escape(data.get("last_summary") or "нет")}</div></div><div class=actions>{action}</div>{run_section}<div class=field><label>История запусков</label><ol>{history or "<li class=empty>История пока пуста</li>"}</ol></div></article></main>'
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
        return HTMLResponse(_board_html(store, workflows, process, request.query_params.get("search", ""), reader, workers.get_limit(), request.query_params.get("view", "wip")))

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

    async def reorder_tickets(request):
        try:
            payload = await request.json()
            process = payload.get("process")
            ticket_ids = payload.get("ticket_ids")
            if not isinstance(process, str) or not isinstance(ticket_ids, list) or not ticket_ids or any(not isinstance(item, str) for item in ticket_ids):
                raise ValueError("Некорректный порядок тикетов")
            tickets = [store.get(ticket_id) for ticket_id in ticket_ids]
            if any(ticket.process != process for ticket in tickets) or len({ticket.status for ticket in tickets}) != 1:
                raise ValueError("Перетаскивать можно только тикеты одной стадии")
            for position, ticket in enumerate(tickets, start=1):
                ticket.priority = position * 10
                store.save(ticket)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"status": "ok", "ticket_ids": ticket_ids})

    async def workers_page(request):
        if request.method == "GET":
            return JSONResponse({"limit": workers.get_limit()})
        try:
            data = _parse_body(await request.body())
            if "delta" in data:
                workers.set_limit(max(0, workers.get_limit() + int(data["delta"])))
            else:
                workers.set_limit(int(data["count"]))
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/?process={urllib.parse.quote(data.get('process', 'discovery'))}", status_code=303)

    async def health(request):
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[
        Route("/", board), Route("/healthz", health), Route("/ticket/{ticket_id}", ticket),
        Route("/new", new_ticket), Route("/tickets/reorder", reorder_tickets, methods=["POST"]), Route("/tickets", create_ticket, methods=["POST"]),
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
