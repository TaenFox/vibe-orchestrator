from __future__ import annotations

import html
import json
import logging
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .config import load_all_workflows
from .control_db import ControlPlaneReader
from .control import DeliverySessionStore, SessionError, WorkerControl
from .orchestrator import decide_human_gate, recover_stale_run, resume_rework, STALE_RUN_TIMEOUT
from .run_store import RunStore
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
.panel { max-width: 900px; padding: 24px; background: #151c24; border: 1px solid #293544; border-radius: 14px; } .panel h2 { margin-top: 0; overflow-wrap: anywhere; } .field { margin: 16px 0; } .field label { display: block; margin-bottom: 6px; color: #94a7b8; font-size: 12px; text-transform: uppercase; } .actions { display: flex; flex-wrap: wrap; gap: 8px; } .empty { color: #94a7b8; } .session-list { display: grid; gap: 10px; max-width: 1000px; } .session-item { display: flex; justify-content: space-between; gap: 16px; align-items: center; padding: 14px; background: #151c24; border: 1px solid #293544; border-radius: 10px; } .session-item:hover { border-color: #65c8f1; } .session-item h3 { margin: 0 0 5px; } .session-actions { display: flex; flex-wrap: wrap; gap: 7px; } .session-actions form { margin: 0; } .member-list { display: grid; gap: 7px; padding: 0; list-style: none; } .member-list li { display: flex; justify-content: space-between; gap: 10px; align-items: center; padding: 9px 11px; background: #1b2631; border-radius: 8px; }
@media (max-width: 800px) { .ticket-table, .ticket-table tbody, .ticket-table tr, .ticket-table td { display: block; } .ticket-table thead { display: none; } .ticket-row { margin: 10px 0; } .ticket-row td { border-left: 1px solid #293544; border-right: 1px solid #293544; border-radius: 0; } .ticket-row td:first-child { border-radius: 9px 9px 0 0; } .ticket-row td:last-child { border-radius: 0 0 9px 9px; } }
"""

STYLE += "\n.blocked-badge { display: inline-block; margin-left: 6px; padding: 2px 5px; border-radius: 999px; font-size: 10px; color: #ffd0c5; background: #713b35; white-space: nowrap; }"
STYLE += "\n.human-gate { margin: 16px 0; padding: 14px; border: 1px solid #c49a4a; border-radius: 10px; background: #3a3020; } .human-gate strong { color: #ffe2a0; } .human-gate .actions { margin-top: 12px; }"


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _parse_body(request_body: bytes) -> dict[str, str]:
    values = urllib.parse.parse_qs(request_body.decode("utf-8"), keep_blank_values=True)
    return {key: items[-1] for key, items in values.items()}


def _resume_rework_if_requested(ticket: Any, target: str) -> None:
    """Treat an explicit move to the session queue as a rework override."""
    if (
        target == "selected_for_session"
        and getattr(ticket, "type", None) == "rework"
        and getattr(ticket, "blocked_reason", None) == "rework_cycle_stopped"
    ):
        ticket.blocked_reason = None


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
    return f"<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Vibe Control</title><style>{STYLE}</style></head><body data-process='{_escape(process)}'><header><h1>VIBE CONTROL</h1><nav>{nav}</nav><a href='/sessions'>Сессии</a><button type=button onclick='location.reload()'>Обновить</button></header>{content}<script>{script}</script></body></html>"


def _ticket_data(ticket: Any) -> dict[str, Any]:
    if hasattr(ticket, "to_dict"):
        return ticket.to_dict()
    payload = ticket.get("payload", {}) if isinstance(ticket, dict) else {}
    data = {**payload, **ticket}
    data["id"] = ticket.get("ticket_id", data.get("id"))
    data["type"] = ticket.get("ticket_type", data.get("type"))
    return data


def _blocker_items(store: TicketStore, blocker_ids: list[str]) -> list[str]:
    items = []
    for blocker_id in blocker_ids:
        try:
            blocker = store.get(blocker_id)
            items.append(f"{blocker_id}: {blocker.title}")
        except KeyError:
            items.append(str(blocker_id))
    return items


def _blocker_badge(store: TicketStore, blocker_ids: list[str]) -> str:
    if not blocker_ids:
        return ""
    items = _blocker_items(store, blocker_ids)
    title = "Ожидает: " + "; ".join(items)
    label = f"ждёт {len(blocker_ids)} завис." if len(blocker_ids) > 1 else "ждёт зависимость"
    return f'<span class=blocked-badge title="{_escape(title)}">{_escape(label)}</span>'


def _human_gate(ticket: Any) -> dict[str, Any] | None:
    gate = ticket.context.get("human_gate") if isinstance(getattr(ticket, "context", None), dict) else None
    return gate if isinstance(gate, dict) and gate.get("status") == "pending" else None


def _human_gate_html(ticket: Any) -> str:
    gate = _human_gate(ticket)
    if not gate:
        return ""
    question = _escape(gate.get("question", "Решение владельца"))
    proposal = _escape(gate.get("proposal", ""))
    agree = _escape(gate.get("agree_label", "Согласиться"))
    disagree = _escape(gate.get("disagree_label", "Не согласиться"))
    return (
        f'<div class="human-gate"><strong>Требуется решение владельца</strong>'
        f'<div class="details-row"><span class="meta">Вопрос</span>{question}</div>'
        f'<div class="details-row"><span class="meta">Предложение агента</span>{proposal}</div>'
        f'<div class="actions"><form method=post action="/ticket/{_escape(ticket.id)}/human-decision"><button name=decision value=agree>{agree}</button></form>'
        f'<form method=post action="/ticket/{_escape(ticket.id)}/human-decision"><button name=decision value=disagree>{disagree}</button></form></div></div>'
    )


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
        stale = False
        if active:
            run = reader.get_run(ticket["active_run"]) if reader else None
            if run and run.get("started_at"):
                stale = datetime.now(timezone.utc) - datetime.fromisoformat(run["started_at"]) >= STALE_RUN_TIMEOUT
        stage = workflow.by_id.get(status)
        attention_reason = ""
        if stage and stage.kind == "human":
            attention_reason = "нужно ваше действие"
        elif ticket.get("blocked_by"):
            attention_reason = "ожидает завершения зависимостей"
        elif ticket.get("blocked_reason"):
            attention_reason = str(ticket.get("blocked_reason"))
        elif stale:
            attention_reason = "запуск завис более 30 минут"
        attention = f'<span class=attention-badge title="{_escape(attention_reason)}">внимание</span>' if attention_reason else ""
        blocked = _blocker_badge(store, list(ticket.get("blocked_by") or []))
        parent = ticket.get("parent") or "—"
        actions = _ticket_actions_html(store, workflows, store.get(ticket_id), stale=stale) if view == "wip" else ""
        rows.append(f'<tr class="ticket-row{" agent-active" if active else ""}{" needs-attention" if attention_reason else ""}" data-ticket="{_escape(ticket_id)}" data-status="{_escape(status)}" draggable="{"true" if view == "wip" else "false"}"><td><a href="/ticket/{_escape(ticket_id)}"><span class=ticket-title>{_escape(ticket.get("title", ""))}{badge}{blocked}{attention}</span><span class=ticket-id>{_escape(ticket_id)} · {_escape(ticket.get("type", ticket.get("ticket_type", "")))}</span></a></td><td><div class=progress aria-label="Прогресс по статусам">{progress}</div><span class=status-label>{_escape(stage.title if stage else status)}</span></td><td class=meta>{_escape(parent)}</td><td class=meta>{_escape(str(ticket.get("updated_at", "")).replace("T", " ")[:16])}</td><td class=row-actions>{actions or "—"}</td></tr>')
    switch = f'<div class=view-switch><a class="{"active" if view == "wip" else ""}" href="/?process={_escape(process)}&view=wip&search={urllib.parse.quote(search)}">WIP</a><a class="{"active" if view == "done" else ""}" href="/?process={_escape(process)}&view=done&search={urllib.parse.quote(search)}">Done</a></div>'
    worker_form = f'<form method=post action=/workers><input type=hidden name=process value="{_escape(process)}"><button name=delta value=-1 aria-label="Уменьшить количество воркеров">−1</button><span class=meta>Воркеры: <strong>{_escape(worker_limit if worker_limit is not None else "?")}</strong></span><button name=delta value=1 aria-label="Увеличить количество воркеров">+1</button></form>' if worker_limit is not None else ""
    table = f'<table class=ticket-table><thead><tr><th>Тикет</th><th>Прогресс</th><th>Родитель</th><th>Обновлён</th><th>Действия</th></tr></thead><tbody>{"".join(rows)}</tbody></table>' if rows else '<div class=empty>В этом представлении тикетов нет</div>'
    content = f'<main><div class=toolbar><form method=get><input name=search value="{_escape(search)}" placeholder="Поиск по тикетам"><input type=hidden name=process value="{_escape(process)}"><input type=hidden name=view value="{_escape(view)}"><button>Найти</button></form>{switch}<a href="/new?process={_escape(process)}">Создать тикет</a>{worker_form}</div>{table}</main>'
    return _layout(content, process)


def _ticket_html(store: TicketStore, workflows: dict, ticket_id: str, reader: ControlPlaneReader | None = None) -> str:
    ticket = store.get(ticket_id)
    data = _ticket_data(reader.get_ticket(ticket_id)) if reader else _ticket_data(ticket)
    data = data or _ticket_data(ticket)
    next_status = next_status_for_ticket(store, ticket)
    action = _ticket_actions_html(store, workflows, ticket, allow_fresh_recovery=True)
    blocker_items = _blocker_items(store, list(data.get("blocked_by") or []))
    blocker_html = "<ul>" + "".join(f"<li>{_escape(item)}</li>" for item in blocker_items) + "</ul>" if blocker_items else "нет"
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
    content = f'<main><article class=panel><a href="/?process={_escape(process)}">← К доске</a><h2>{_escape(data.get("title", ""))}</h2><div class=meta>{_escape(data.get("id", ticket.id))} · {_escape(data.get("type", ""))} · {_escape(data.get("status", ""))} · приоритет {_escape(data.get("priority", 100))}</div><div class=field><label>Описание</label><div class=summary>{_escape(data.get("description") or "(пусто)")}</div></div><div class=field><label>Блокировки</label><div class=summary>{blocker_html}</div></div><div class=field><label>Последний результат</label><div class=summary>{_escape(data.get("last_summary") or "нет")}</div></div><div class=actions>{action}</div>{run_section}<div class=field><label>История запусков</label><ol>{history or "<li class=empty>История пока пуста</li>"}</ol></div></article></main>'
    return _layout(content, process)


def _ticket_actions_html(store: TicketStore, workflows: dict, ticket: Any, *, stale: bool | None = None,
                         allow_fresh_recovery: bool = False) -> str:
    if _human_gate(ticket):
        return _human_gate_html(ticket)
    workflow = workflows[ticket.process]
    next_status = next_status_for_ticket(store, ticket)
    action = f'<form method=post action="/ticket/{_escape(ticket.id)}/move"><input type=hidden name=target value="{_escape(next_status)}"><button>Перевести в {_escape(workflow.by_id[next_status].title)}</button></form>' if next_status else ""
    if ticket.type == "rework" and ticket.blocked_reason == "rework_cycle_stopped":
        action += f'<form method=post action="/ticket/{_escape(ticket.id)}/resume-rework"><button>Разрешить ещё один проход реворка</button></form>'
    run = None
    if stale is None and ticket.active_run:
        run = RunStore(store.database).get(ticket.active_run)
        stale = bool(run and run.get("state") == "started" and run.get("started_at") and datetime.now(timezone.utc) - datetime.fromisoformat(run["started_at"]) >= STALE_RUN_TIMEOUT)
    if stale or (allow_fresh_recovery and ticket.active_run and run and run.get("state") == "started"):
        label = "Восстановить зависший запуск" if stale else "Прервать и восстановить запуск"
        action += f'<form method=post action="/ticket/{_escape(ticket.id)}/recover-stale-run"><button>{label}</button></form>'
    return action


def _new_html(process: str) -> str:
    content = f'<main><article class=panel><a href="/?process={_escape(process)}">← К доске</a><h2>Новый тикет</h2><form method=post action=/tickets><input type=hidden name=process value="{_escape(process)}"><div class=field><label>Тип</label><input name=type required value="idea"></div><div class=field><label>Заголовок</label><input name=title required autofocus></div><div class=field><label>Описание</label><textarea name=description rows=8></textarea></div><div class=field><label>Приоритет</label><input name=priority type=number min=0 value=100></div><div class=actions><button type=submit>Создать</button></div></form></article></main>'
    return _layout(content, process)


def _sessions_html(session_store: DeliverySessionStore) -> str:
    items = []
    for session in session_store.list():
        items.append(f'<a class=session-item href="/sessions/{_escape(session.id)}"><div><h3>{_escape(session.title or session.id)}</h3><span class=meta>{_escape(session.id)} · {_escape(session.status)}</span></div><span class=meta>{len(session.participants)} тикетов</span></a>')
    content = f'<main><div class=toolbar><h2>Delivery-сессии</h2><a href="/sessions/new">Новая сессия</a></div><div class=session-list>{"".join(items) or "<div class=empty>Сессий пока нет</div>"}</div></main>'
    return _layout(content, "delivery")


def _session_detail_html(session_store: DeliverySessionStore, store: TicketStore, session_id: str) -> str:
    session = session_store.get(session_id)
    members = []
    for ticket_id in session_store.store.effective_ticket_ids(session):
        try:
            ticket = store.get(ticket_id)
            remove = f'<form method=post action="/sessions/{_escape(session.id)}/remove"><input type=hidden name=ticket_id value="{_escape(ticket.id)}"><button>Убрать</button></form>' if session.status == "draft" else ""
            members.append(f'<li><div><strong>{_escape(ticket.title)}</strong><br><span class=meta>{_escape(ticket.id)} · {_escape(ticket.status)}</span></div>{remove}</li>')
        except KeyError:
            members.append(f'<li><span>{_escape(ticket_id)} · тикет не найден</span></li>')
    available = [ticket for ticket in store.list("delivery") if ticket.id not in session_store.store.effective_ticket_ids(session) and not store.is_done(ticket)]
    add_form = ""
    if session.status == "draft":
        options = "".join(f'<option value="{_escape(ticket.id)}">{_escape(ticket.id)} · {_escape(ticket.title)}</option>' for ticket in available)
        add_form = f'<div class=field><label>Добавить тикет</label><form method=post action="/sessions/{_escape(session.id)}/add"><select name=ticket_id required><option value="">Выберите тикет</option>{options}</select><button>Добавить</button></form></div>'
    lifecycle = []
    if session.status == "draft":
        lifecycle.append(f'<form method=post action="/sessions/{_escape(session.id)}/activate"><button>Запустить сессию</button></form>')
    if session.status == "active":
        lifecycle.extend([f'<form method=post action="/sessions/{_escape(session.id)}/complete"><input name=reason placeholder="Причина override, если нужно"><button>Завершить</button></form>', f'<form method=post action="/sessions/{_escape(session.id)}/cancel"><input name=reason placeholder="Причина отмены"><button>Отменить</button></form>'])
    content = f'<main><article class=panel><a href="/sessions">← К списку сессий</a><h2>{_escape(session.title or session.id)}</h2><div class=meta>{_escape(session.id)} · статус: {_escape(session.status)} · тикетов: {len(session.participants)}</div><div class=field><label>Lifecycle</label><div class=actions>{"".join(lifecycle) or "<span class=empty>Действий нет</span>"}</div></div>{add_form}<div class=field><label>Состав</label><ul class=member-list>{"".join(members) or "<li class=empty>Состав пуст</li>"}</ul></div></article></main>'
    return _layout(content, "delivery")


def _new_session_html() -> str:
    content = '<main><article class=panel><a href="/sessions">← К списку сессий</a><h2>Новая сессия</h2><form method=post action="/sessions"><div class=field><label>Название</label><input name=title required autofocus></div><div class=actions><button type=submit>Создать</button></div></form></article></main>'
    return _layout(content, "delivery")


def create_app(project: str | Path) -> Starlette:
    root = Path(project).resolve()
    store = TicketStore(root)
    store.init()
    workflows = load_all_workflows()
    workers = WorkerControl(root)
    session_store = DeliverySessionStore(root)
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

    async def sessions_page(request):
        if request.method == "GET":
            return HTMLResponse(_sessions_html(session_store))
        try:
            data = _parse_body(await request.body())
            session = session_store.create(data["title"])
        except (KeyError, SessionError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/sessions/{urllib.parse.quote(session.id)}", status_code=303)

    async def new_session(request):
        return HTMLResponse(_new_session_html())

    async def session_detail(request):
        try:
            return HTMLResponse(_session_detail_html(session_store, store, request.path_params["session_id"]))
        except SessionError as exc:
            return Response(str(exc), status_code=404)

    async def session_action(request):
        session_id = request.path_params["session_id"]
        action = request.path_params["action"]
        data = _parse_body(await request.body())
        try:
            if action == "add":
                session_store.add(session_id, data["ticket_id"], store)
            elif action == "remove":
                session_store.remove(session_id, data["ticket_id"])
            elif action == "activate":
                session_store.activate(session_id, store)
            elif action == "complete":
                session_store.complete(session_id, store, data.get("reason") or None)
            elif action == "cancel":
                session_store.cancel(session_id, store, data.get("reason") or None)
            else:
                return Response("Unknown session action", status_code=404)
        except (KeyError, SessionError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/sessions/{urllib.parse.quote(session_id)}", status_code=303)

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
            _resume_rework_if_requested(ticket, target)
            ticket.status = target
            store.save(ticket)
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/?process={ticket.process}", status_code=303)

    async def resume_rework_ticket(request):
        try:
            ticket = resume_rework(store, request.path_params["ticket_id"])
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/ticket/{urllib.parse.quote(ticket.id)}", status_code=303)

    async def human_decision_ticket(request):
        try:
            decision = _parse_body(await request.body()).get("decision", "")
            ticket = decide_human_gate(store, request.path_params["ticket_id"], decision)
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/ticket/{urllib.parse.quote(ticket.id)}", status_code=303)

    async def recover_stale_run_ticket(request):
        try:
            ticket = recover_stale_run(store, request.path_params["ticket_id"], force=True)
        except (KeyError, ValueError) as exc:
            return Response(str(exc), status_code=400)
        return RedirectResponse(f"/ticket/{urllib.parse.quote(ticket.id)}", status_code=303)

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
        Route("/new", new_ticket), Route("/sessions", sessions_page, methods=["GET", "POST"]), Route("/sessions/new", new_session), Route("/sessions/{session_id}", session_detail), Route("/sessions/{session_id}/{action}", session_action, methods=["POST"]),
        Route("/tickets/reorder", reorder_tickets, methods=["POST"]), Route("/tickets", create_ticket, methods=["POST"]),
        Route("/ticket/{ticket_id}/move", move_ticket, methods=["POST"]), Route("/ticket/{ticket_id}/resume-rework", resume_rework_ticket, methods=["POST"]), Route("/ticket/{ticket_id}/human-decision", human_decision_ticket, methods=["POST"]), Route("/ticket/{ticket_id}/recover-stale-run", recover_stale_run_ticket, methods=["POST"]),
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
