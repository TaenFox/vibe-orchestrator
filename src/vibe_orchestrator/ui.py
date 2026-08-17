from __future__ import annotations

import html
import json
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from .config import load_all_workflows
from .control import DeliverySessionStore, SessionError, WorkerControl
from .git_trees import GitTreeManager
from .tickets import TicketStore, automatic_retry_available, next_status_for_ticket, reset_failed_retry, retry_exhausted
from .token_usage import is_confirmed_token_usage, unknown_token_usage

CSS = """:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#e6edf3;background:#0d1117}*{box-sizing:border-box}body{margin:0;overflow-x:hidden}header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 18px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117;z-index:2}header form{display:flex;gap:7px;align-items:center;flex-wrap:wrap}header button{margin-top:0}input,select,textarea{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:6px;max-width:100%;font:inherit}input[type=number]{width:52px}.create-form{display:flex;flex-wrap:wrap;gap:7px;align-items:center;padding:12px 14px;border-bottom:1px solid #30363d}.create-form input[name=title],.create-form textarea[name=description]{min-width:220px}.create-form textarea{min-height:34px;resize:vertical}a{color:#58a6ff;text-decoration:none}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid #f0c674;outline-offset:2px}.board-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #30363d}.board{display:flex;gap:12px;padding:14px;align-items:flex-start;overflow-x:auto;min-height:calc(100vh - 120px)}.compact-group{display:flex;flex-direction:column;gap:8px;width:260px;min-width:260px}.compact-group>.column{width:100%;min-width:0}.column{width:260px;min-width:260px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}.column h3{font-size:13px;margin:0 0 10px;color:#8b949e;text-transform:uppercase}.card{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:10px;margin-bottom:9px}.card strong{display:block;font-size:14px;margin:4px 0;overflow-wrap:anywhere}.meta{color:#8b949e;font-size:12px}.badge{display:inline-block;border:1px solid #30363d;border-radius:999px;padding:2px 6px;font-size:11px;margin-right:4px}button{background:#238636;color:white;border:0;border-radius:6px;padding:6px 8px;cursor:pointer;margin-top:8px}.summary{margin-top:7px;color:#c9d1d9;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}.details{margin-top:8px;border-top:1px solid #30363d;padding-top:8px}.details summary{cursor:pointer;color:#58a6ff;font-size:12px}.details-body{margin-top:8px;display:grid;gap:6px}.details-row{font-size:12px;color:#c9d1d9;white-space:pre-wrap;overflow-wrap:anywhere}.details-row .meta{display:block;margin-bottom:2px}.flat-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));align-items:start;overflow-x:hidden}.flat-list .column{width:auto;min-width:0}.board-empty{padding:24px;color:#8b949e}@media (max-width:700px){header{gap:10px;padding:10px}header form,.create-form,.board-toolbar{width:100%}.create-form input,.create-form select,.create-form textarea{flex:1 1 100%;min-width:0}.board{padding:10px;gap:8px}.compact-group{width:min(260px,calc(100vw - 20px));min-width:min(260px,calc(100vw - 20px))}.column{width:min(260px,calc(100vw - 20px));min-width:min(260px,calc(100vw - 20px))}}"""
AUTO_REFRESH_SECONDS = 5
AUTO_REFRESH_SCRIPT = f"""<script>
(() => {{
  const key = 'vibe-board-state';
  const storage = {{ get: () => {{ try {{ return sessionStorage.getItem(key) || '{{}}'; }} catch (_) {{ return '{{}}'; }} }}, set: value => {{ try {{ sessionStorage.setItem(key, value); }} catch (_) {{ /* storage can be disabled */ }} }} }};
  const state = () => {{ try {{ return JSON.parse(storage.get()); }} catch (_) {{ return {{}}; }} }};
  const save = (extra = {{}}) => storage.set(JSON.stringify({{...state(), process: new URLSearchParams(location.search).get('process') || 'discovery', ...extra}}));
  const controls = () => [...document.querySelectorAll('input,select,textarea')];
  const controlKey = (el) => el.dataset.boardStateKey || `${{el.form?.getAttribute('action') || ''}}:${{el.name || el.type || el.tagName.toLowerCase()}}`;
  const inputState = () => controls().map(el => [controlKey(el), el.value]);
  const detailsState = () => [...document.querySelectorAll('details[data-ticket-details]')].map(el => [el.dataset.ticketDetails, el.open]);
  const restore = () => {{
    const restored = state();
    const savedInputs = new Map(restored.inputs || []);
    controls().forEach(el => {{ const value = savedInputs.get(controlKey(el)); if (value !== undefined && !el.matches(':focus')) el.value = value; }});
    const savedDetails = new Map(restored.details || []);
    document.querySelectorAll('details[data-ticket-details]').forEach(el => {{ if (savedDetails.has(el.dataset.ticketDetails)) el.open = savedDetails.get(el.dataset.ticketDetails); }});
  }};
  const remember = () => {{
    const active = document.activeElement;
    const board = document.querySelector('.board');
    save({{mode: document.querySelector('[data-board-mode]')?.value || 'compact', ticket: document.querySelector('.card[data-ticket].selected')?.dataset.ticket || state().ticket, boardScrollX: board?.scrollLeft || 0, scrollY: window.scrollY || document.scrollingElement?.scrollTop || 0, inputs: inputState(), details: detailsState()}});
    if (active) save({{focus: controls().indexOf(active)}});
  }};
  const refresh = async (force = false) => {{
    if (document.hidden || document.querySelector('details[open]') || (!force && document.activeElement?.matches('input, select, textarea'))) return;
    remember(); const current = state();
    const params = new URLSearchParams({{process: current.process || 'discovery', mode: current.mode || 'compact', search: current.search || '', status: current.status || ''}});
    try {{ const response = await fetch('/fragment?' + params); if (!response.ok) return; const fragment = await response.text();
      const board = document.querySelector('.board'); if (!board) return; board.outerHTML = fragment;
      const restored = state(); restore();
      if (restored.ticket) document.querySelector(`.card[data-ticket="${{CSS.escape(restored.ticket)}}"]`)?.classList.add('selected');
      const refreshedBoard = document.querySelector('.board'); if (refreshedBoard && restored.boardScrollX != null) refreshedBoard.scrollLeft = restored.boardScrollX; if (restored.scrollY != null) window.scrollTo(0, restored.scrollY); if (restored.focus >= 0) controls()[restored.focus]?.focus();
    }} catch (_) {{ /* transient server/network failure: keep the current board */ }}
  }};
  document.addEventListener('input', event => {{ if (event.target.matches('input,select,textarea')) remember(); }});
  document.addEventListener('change', event => {{ if (event.target.matches('[data-board-mode], [data-board-search], [data-board-status]')) {{ const value = event.target.value; save(event.target.matches('[data-board-mode]') ? {{mode:value}} : event.target.matches('[data-board-search]') ? {{search:value}} : {{status:value}}); refresh(true); }} }});
  document.addEventListener('toggle', event => {{ if (event.target.matches('details[data-ticket-details]')) remember(); }}, true);
  document.addEventListener('click', event => {{ const link = event.target.closest('a[href*="?process="]'); if (link) {{ remember(); save({{process: new URL(link.href, location.href).searchParams.get('process')}}); }} const card = event.target.closest('.card[data-ticket]'); if (card) {{ document.querySelectorAll('.card.selected').forEach(item => item.classList.remove('selected')); card.classList.add('selected'); save({{ticket: card.dataset.ticket}}); }} }});
  const current = state(); const modeControl = document.querySelector('[data-board-mode]'); const searchControl = document.querySelector('[data-board-search]'); const statusControl = document.querySelector('[data-board-status]'); if (modeControl && current.mode) modeControl.value = current.mode; if (searchControl && current.search) searchControl.value = current.search; if (statusControl && current.status) statusControl.value = current.status; restore(); if (current.ticket) document.querySelector(`.card[data-ticket="${{CSS.escape(current.ticket)}}"]`)?.classList.add('selected'); const initialBoard = document.querySelector('.board'); if (initialBoard && current.boardScrollX != null) initialBoard.scrollLeft = current.boardScrollX; if (current.scrollY != null) window.scrollTo(0, current.scrollY); setInterval(refresh, {AUTO_REFRESH_SECONDS * 1000});
}})();
</script>"""


def _build_server(project: Path, host: str, port: int) -> ThreadingHTTPServer:
    store = TicketStore(project); store.init(); workflows = load_all_workflows(); worker_control = WorkerControl(project); tree_manager = GitTreeManager(project, store); session_store = DeliverySessionStore(project)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                query = urllib.parse.parse_qs(parsed.query); process = query.get("process", ["discovery"])[0]
                mode = query.get("mode", ["compact"])[0]; search = query.get("search", [""])[0]; status = query.get("status", [""])[0]
                return self._html(render_board(store, workflows, process, worker_control, tree_manager, session_store, mode=mode, search=search, status=status))
            if parsed.path == "/fragment":
                query = urllib.parse.parse_qs(parsed.query)
                return self._html(render_board_fragment(store, workflows, query.get("process", ["discovery"])[0], worker_control, tree_manager, session_store, mode=query.get("mode", ["compact"])[0], search=query.get("search", [""])[0], status=query.get("status", [""])[0]))
            if parsed.path == "/api/tickets": return self._json([_ticket_payload(ticket) for ticket in store.list()])
            if parsed.path == "/api/sessions": return self._json([_session_payload(item, store) for item in session_store.list()])
            if parsed.path.startswith("/api/sessions/"):
                try:
                    return self._json(_session_payload(session_store.get(parsed.path.rsplit("/", 1)[-1]), store))
                except SessionError as exc:
                    return self.send_error(404, str(exc))
            self.send_error(404)
        def do_POST(self):
            length = int(self.headers.get("content-length", "0")); data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            try:
                if self.path == "/session/create":
                    session_store.create(data.get("title", [""])[0]); return self._redirect("/?process=delivery")
                if self.path == "/session/add":
                    session_store.add(data["session"][0], data["ticket"][0], store); return self._redirect("/?process=delivery")
                if self.path == "/session/remove":
                    session_store.remove(data["session"][0], data["ticket"][0]); return self._redirect("/?process=delivery")
                if self.path == "/session/activate":
                    session_store.activate(data["session"][0], store); return self._redirect("/?process=delivery")
                if self.path in {"/session/complete", "/session/cancel"}:
                    session_id = data["session"][0]; reason = data.get("override", [None])[0]
                    if self.path.endswith("complete"):
                        session_store.complete(session_id, store, reason)
                    else:
                        session_store.cancel(session_id, store, reason)
                    return self._redirect("/?process=delivery")
            except (KeyError, SessionError):
                return self.send_error(400, "Некорректная операция сессии")
            if self.path == "/create":
                try:
                    process = data["process"][0]
                    ticket_type = data["type"][0].strip()
                    title = data["title"][0].strip()
                    description = data.get("description", [""])[0].strip()
                    priority = int(data.get("priority", ["100"])[0])
                    parent = data.get("parent", [""])[0].strip() or None
                    if process not in workflows or not ticket_type or not title or priority < 0:
                        raise ValueError
                    ticket = store.create(process, ticket_type, title, description=description, priority=priority, parent=parent)
                except (KeyError, ValueError):
                    return self.send_error(400, "Некорректные данные тикета")
                return self._redirect(f"/?process={urllib.parse.quote(ticket.process)}")
            if self.path == "/move":
                ticket = store.get(data["id"][0]); workflow = workflows[ticket.process]; target = next_status_for_ticket(store, ticket); requested = data.get("target", [target])[0]
                if not target or requested != target or target not in workflow.by_id: return self.send_error(400, "Переход недоступен")
                ticket.status = target; store.save(ticket); return self._redirect(f"/?process={ticket.process}")
            if self.path == "/retry":
                ticket = store.get(data["id"][0])
                if not reset_failed_retry(ticket): return self.send_error(400, "Повтор недоступен")
                store.save(ticket); return self._redirect(f"/?process={ticket.process}")
            if self.path == "/workers":
                try:
                    limit = int(data["count"][0])
                    worker_control.set_limit(limit)
                except (KeyError, ValueError):
                    return self.send_error(400, "Некорректное количество воркеров")
                process = data.get("process", ["discovery"])[0]
                return self._redirect(f"/?process={urllib.parse.quote(process)}")
            if self.path == "/release-retry":
                ticket = store.get(data["id"][0])
                if not tree_manager.reset_integration(ticket.id): return self.send_error(400, "Повтор интеграции недоступен")
                ticket.last_outcome = None; ticket.last_summary = None; store.save(ticket)
                return self._redirect(f"/?process={ticket.process}")
            self.send_error(404)
        def log_message(self, fmt, *args): return
        def _html(self, text):
            payload=text.encode("utf-8"); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def _json(self,obj):
            payload=json.dumps(obj,ensure_ascii=False,indent=2).encode("utf-8"); self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def _redirect(self,location): self.send_response(303); self.send_header("Location",location); self.end_headers()

    return ThreadingHTTPServer((host, port), Handler)


def start_server(project: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> tuple[ThreadingHTTPServer, Thread]:
    server = _build_server(project, host, port)
    url = f"http://{host}:{port}"
    print(f"Интерфейс: {url}")
    if open_browser: webbrowser.open(url)
    thread = Thread(target=server.serve_forever, name="vibe-ui", daemon=True)
    thread.start()
    return server, thread


def serve(project: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    server = _build_server(project, host, port)
    url = f"http://{host}:{port}"
    print(f"Интерфейс: {url}")
    if open_browser: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


def render_board(store, workflows, process: str, worker_control: WorkerControl | None = None, tree_manager: GitTreeManager | None = None, session_store: DeliverySessionStore | None = None, *, mode: str = "compact", search: str = "", status: str = "") -> str:
    workflow=workflows.get(process) or workflows["discovery"]; tickets=store.list(workflow.id)
    mode = mode if mode in {"compact", "flat"} else "compact"
    search = search.strip()
    if search:
        needle = search.casefold(); tickets = [t for t in tickets if needle in f"{t.id} {t.title} {t.description}".casefold()]
    if status and status in workflow.by_id:
        tickets = [t for t in tickets if t.status == status]
    nav=" ".join(f'<a href="/?process={p.id}">{html.escape(p.title)}</a>' for p in workflows.values())
    columns=[]
    rendered = set()
    for stage in workflow.stages:
        if stage.id in rendered or (mode == "flat" and stage.kind != "queue"):
            continue
        related = [stage]
        if mode == "compact" and stage.kind == "queue" and stage.pull_to and stage.pull_to in workflow.by_id:
            related.append(workflow.by_id[stage.pull_to])
        rendered.update(item.id for item in related)
        group = []
        for item in related:
            group.append(_stage_column(store, workflow, item, tickets, tree_manager, session_store))
        columns.append(f'<div class="compact-group" data-stage-group="{html.escape(stage.id)}">{"".join(group)}</div>' if mode == "compact" else "".join(group))
    if mode == "flat":
        columns = [_stage_column(store, workflow, stage, tickets, tree_manager, session_store) for stage in workflow.stages]
    board_class = "board flat-list" if mode == "flat" else "board"
    toolbar = _board_toolbar(workflow, mode, search, status)
    board = f'<main class="{board_class}">{"".join(columns) or "<div class=board-empty>Нет тикетов по текущему фильтру</div>"}</main>'
    refresh_hint = f"частичное автообновление {AUTO_REFRESH_SECONDS}с"
    worker_control = worker_control or WorkerControl(store.project)
    worker_limit = worker_control.get_limit()
    active_workers = sum(1 for ticket in store.list() if ticket.active_run)
    worker_form = f'<form method="post" action="/workers"><input type="hidden" name="process" value="{html.escape(workflow.id)}"><label class="meta">воркеры <input type="number" name="count" min="0" value="{worker_limit}"></label><button>Применить</button><span class="meta">активно {active_workers}</span></form>'
    process_options = "".join(f'<option value="{html.escape(item.id)}"{(" selected" if item.id == workflow.id else "")}>{html.escape(item.title)}</option>' for item in workflows.values())
    create_form = (
        '<form class="create-form" method="post" action="/create">'
        '<strong>Новый тикет</strong>'
        f'<select name="process">{process_options}</select>'
        '<input name="type" placeholder="тип, например idea" required>'
        '<input name="title" placeholder="заголовок" required>'
        '<textarea name="description" placeholder="описание" rows="1"></textarea>'
        '<input name="priority" type="number" min="0" value="100" title="Приоритет">'
        '<input name="parent" placeholder="родительский ID, необязательно">'
        '<button>Создать</button></form>'
    )
    sessions_html = _sessions_html(store, session_store) if process == "delivery" else ""
    return f'<!doctype html><html><head><meta charset="utf-8"><title>vibe · {html.escape(workflow.title)}</title><style>{CSS}</style>{AUTO_REFRESH_SCRIPT}</head><body><header><strong>vibe-orchestrator</strong>{nav}{worker_form}<span class="meta">{html.escape(str(store.project))}</span><span class="meta">{html.escape(refresh_hint)}</span></header>{create_form}{sessions_html}{toolbar}{board}</body></html>'


def render_board_fragment(store, workflows, process: str, worker_control=None, tree_manager=None, session_store=None, *, mode="compact", search="", status="") -> str:
    workflow = workflows.get(process) or workflows["discovery"]
    tickets = store.list(workflow.id)
    mode = mode if mode in {"compact", "flat"} else "compact"
    needle = search.strip().casefold()
    if needle:
        tickets = [t for t in tickets if needle in f"{t.id} {t.title} {t.description}".casefold()]
    if status and status in workflow.by_id:
        tickets = [t for t in tickets if t.status == status]
    stages = workflow.stages
    if mode == "compact":
        columns=[]; rendered=set()
        for stage in stages:
            if stage.id in rendered: continue
            related=[stage]
            if stage.kind == "queue" and stage.pull_to and stage.pull_to in workflow.by_id: related.append(workflow.by_id[stage.pull_to])
            rendered.update(item.id for item in related)
            columns.append(f'<div class="compact-group" data-stage-group="{html.escape(stage.id)}">{"".join(_stage_column(store, workflow, item, tickets, tree_manager, session_store) for item in related)}</div>')
    else:
        columns=[_stage_column(store, workflow, stage, tickets, tree_manager, session_store) for stage in stages]
    return f'<main class="{"board flat-list" if mode == "flat" else "board"}">{"".join(columns) or "<div class=board-empty>Нет тикетов по текущему фильтру</div>"}</main>'


def _board_toolbar(workflow, mode, search, status):
    options = '<option value="">Все стадии</option>' + ''.join(f'<option value="{html.escape(stage.id)}"{(" selected" if stage.id == status else "")}>{html.escape(stage.title)}</option>' for stage in workflow.stages)
    return f'<section class="board-toolbar"><label>Режим <select data-board-mode><option value="compact"{(" selected" if mode == "compact" else "")}>Компактный</option><option value="flat"{(" selected" if mode == "flat" else "")}>Плоский список</option></select></label><label>Поиск <input data-board-search type="search" value="{html.escape(search)}" placeholder="ID, заголовок или описание"></label><label>Фильтр <select data-board-status>{options}</select></label></section>'


def _stage_column(store, workflow, stage, tickets, tree_manager, session_store):
        cards=[]; stage_tickets=[t for t in tickets if t.status==stage.id]; stage_tickets.sort(key=lambda t:(0 if t.wip_exempt else 1,t.priority,t.created_at))
        for ticket in stage_tickets:
            action=""
            target = next_status_for_ticket(store, ticket)
            if target:
                action=f'<form method="post" action="/move"><input type="hidden" name="id" value="{html.escape(ticket.id)}"><input type="hidden" name="target" value="{html.escape(target)}"><button>Переместить → {html.escape(workflow.by_id[target].title)}</button></form>'
            if stage.kind == "agent" and retry_exhausted(ticket) and not ticket.blocked_by:
                action=f'<form method="post" action="/retry"><input type="hidden" name="id" value="{html.escape(ticket.id)}"><button>Повторить</button></form>'
            if ticket.status == "ready_for_release" and ticket.last_outcome == "integration_conflict":
                action=f'<form method="post" action="/release-retry"><input type="hidden" name="id" value="{html.escape(ticket.id)}"><button>Повторить интеграцию</button></form>'
            blocked=f'<span class="badge">заблокирован: {len(ticket.blocked_by)}</span>' if ticket.blocked_by else ""; run='<span class="badge">агент выполняется</span>' if ticket.active_run else ""; retry='<span class="badge">ожидает автоповтора</span>' if stage.kind == "agent" and automatic_retry_available(ticket) and not ticket.active_run else ""; corrective='<span class="badge">без учета WIP</span>' if ticket.wip_exempt else ""; session_badge=_ticket_session_badge(ticket, session_store); summary=f'<div class="summary">{html.escape(ticket.last_summary or "")}</div>' if ticket.last_summary else ""; tree=tree_manager.trees.get(ticket.id) if tree_manager else None; details=_ticket_details_html(ticket, tree)
            cards.append(f'<div class="card" data-ticket="{html.escape(ticket.id)}"><span class="meta">{html.escape(ticket.id)}</span><strong>{html.escape(ticket.title)}</strong><span class="badge">{html.escape(ticket.type)}</span>{session_badge}{corrective}{blocked}{run}{retry}<div class="meta">приоритет {ticket.priority}</div>{summary}{details}{action}</div>')
        wip=f" · WIP {stage.wip}" if stage.wip is not None else ""; return f'<section class="column" data-stage="{html.escape(stage.id)}"><h3>{html.escape(stage.title)}{wip}</h3>{"".join(cards)}</section>'


def _session_payload(session, store) -> dict:
    tickets = [store.get(ticket_id) for ticket_id in session.participants if _ticket_exists(store, ticket_id)]
    aggregate = {
        "mandatory": sum(ticket.mandatory for ticket in tickets),
        "optional": sum(not ticket.mandatory for ticket in tickets),
        "done": sum(store.is_done(ticket) for ticket in tickets),
        "blocked": sum(bool(ticket.blocked_by) for ticket in tickets),
        "active_run": sum(bool(ticket.active_run) for ticket in tickets),
    }
    return {"session": session.to_dict(), "aggregate": aggregate, "tickets": [_ticket_payload(ticket) for ticket in tickets]}


def _ticket_exists(store, ticket_id: str) -> bool:
    try:
        store.get(ticket_id)
    except KeyError:
        return False
    return True


def _ticket_session_badge(ticket, session_store) -> str:
    if not session_store or ticket.process != "delivery":
        return ""
    session = next((item for item in session_store.list() if ticket.id in item.participants), None)
    return f'<span class="badge">сессия: {html.escape(session.id)}</span>' if session else ""


def _ticket_usage(ticket) -> tuple[dict, dict]:
    terminal = [entry for entry in ticket.run_history if entry.get("event") in {"completed", "failed"}]
    confirmed = [entry.get("token_usage") for entry in terminal if is_confirmed_token_usage(entry.get("token_usage"))]
    latest_candidate = terminal[-1].get("token_usage") if terminal else None
    latest = latest_candidate if is_confirmed_token_usage(latest_candidate) else unknown_token_usage()
    return latest, {
        "confirmed_runs": len(confirmed),
        "input_tokens": sum(usage["input_tokens"] for usage in confirmed),
        "output_tokens": sum(usage["output_tokens"] for usage in confirmed),
        "total_tokens": sum(usage["total_tokens"] for usage in confirmed),
        "latest_captured_at": latest["captured_at"],
    }


def _ticket_payload(ticket) -> dict:
    payload = ticket.to_dict()
    latest, aggregate = _ticket_usage(ticket)
    if aggregate["confirmed_runs"] or ticket.run_history:
        payload["token_usage"] = latest
        payload["token_usage_aggregate"] = aggregate
    return payload


def _sessions_html(store, session_store) -> str:
    if not session_store:
        session_store = DeliverySessionStore(store.project)
    sessions = session_store.list()
    cards = []
    for session in sessions:
        payload = _session_payload(session, store)
        aggregate = payload["aggregate"]
        participants = []
        for ticket_id in session.participants:
            ticket = next((item for item in payload["tickets"] if item["id"] == ticket_id), None)
            title = ticket["title"] if ticket else "тикет не найден"
            remove = ""
            if session.status == "draft":
                remove = f'<form method="post" action="/session/remove"><input type="hidden" name="session" value="{html.escape(session.id)}"><input type="hidden" name="ticket" value="{html.escape(ticket_id)}"><button>Убрать</button></form>'
            participants.append(f'<div class="details-row"><span class="meta">{html.escape(ticket_id)}</span>{html.escape(title)}{remove}</div>')
        actions = ""
        if session.status == "draft":
            options = "".join(
                f'<option value="{html.escape(ticket.id)}">{html.escape(ticket.id)} · {html.escape(ticket.title)}</option>'
                for ticket in store.list("delivery")
                if not store.is_done(ticket) and ticket.id not in session.participants
            )
            actions = f'<form method="post" action="/session/add"><input type="hidden" name="session" value="{html.escape(session.id)}"><select name="ticket">{options}</select><button>Добавить тикет</button></form><form method="post" action="/session/activate"><input type="hidden" name="session" value="{html.escape(session.id)}"><button>Активировать</button></form>'
        elif session.status == "active":
            actions = f'<form method="post" action="/session/complete"><input type="hidden" name="session" value="{html.escape(session.id)}"><input name="override" placeholder="Причина override, если неполна"><button>Завершить</button></form><form method="post" action="/session/cancel"><input type="hidden" name="session" value="{html.escape(session.id)}"><input name="override" placeholder="Причина override, если неполна"><button>Отменить</button></form>'
        cards.append(f'<article class="card"><strong>{html.escape(session.title)}</strong><span class="badge">{html.escape(session.id)}</span><span class="badge">{html.escape(session.status)}</span><div class="meta">mandatory {aggregate["mandatory"]} · optional {aggregate["optional"]} · done {aggregate["done"]} · blocked {aggregate["blocked"]} · active_run {aggregate["active_run"]}</div><div class="details-body">{"".join(participants) or "<span class=meta>Состав пуст</span>"}</div>{actions}</article>')
    create = '<form class="create-form" method="post" action="/session/create"><strong>Новая Delivery-сессия</strong><input name="title" placeholder="название сессии" required><button>Создать</button></form>'
    return f'<section class="sessions"><h2>Delivery-сессии</h2>{create}{"".join(cards) or "<div class=meta>Сессий пока нет</div>"}</section>'


def _ticket_details_html(ticket, tree=None) -> str:
    parent = ticket.parent or "нет"
    blockers = ", ".join(ticket.blocked_by) if ticket.blocked_by else "нет"
    outcome = ticket.last_outcome or "нет"
    retry_after = ticket.retry_after or "нет"
    description = ticket.description or "(пусто)"
    latest_usage, aggregate = _ticket_usage(ticket)
    usage_text = "unknown"
    usage_time = "нет"
    if is_confirmed_token_usage(latest_usage):
        usage_text = f'{latest_usage["total_tokens"]} (input {latest_usage["input_tokens"]} · output {latest_usage["output_tokens"]})'
        usage_time = latest_usage["captured_at"] or "неизвестно"
    tree_details = ""
    if tree:
        tree_details = (
            f'<div class="details-row"><span class="meta">Ветка</span>{html.escape(tree.branch)}</div>'
            f'<div class="details-row"><span class="meta">Worktree</span>{html.escape(tree.worktree)}</div>'
            f'<div class="details-row"><span class="meta">Интеграция</span>{html.escape(tree.integration_status)}</div>'
        )
    return (
        f'<details class="details" data-ticket-details="{html.escape(ticket.id)}"><summary>Подробнее</summary><div class="details-body">'
        f'<div class="details-row"><span class="meta">Описание</span>{html.escape(description)}</div>'
        f'<div class="details-row"><span class="meta">Родитель</span>{html.escape(parent)}</div>'
        f'<div class="details-row"><span class="meta">Блокирует</span>{html.escape(blockers)}</div>'
        f'<div class="details-row"><span class="meta">Последний outcome</span>{html.escape(outcome)}</div>'
        f'<div class="details-row"><span class="meta">Ошибок подряд</span>{ticket.consecutive_failures}</div>'
        f'<div class="details-row"><span class="meta">Повтор после</span>{html.escape(retry_after)}</div>'
        f'<div class="details-row"><span class="meta">Токены (актуальный источник)</span>{html.escape(usage_text)} · {html.escape(usage_time)}</div>'
        f'<div class="details-row"><span class="meta">Токены (подтвержденные запуски)</span>{aggregate["total_tokens"]} · запусков {aggregate["confirmed_runs"]}</div>'
        f'{tree_details}'
        f'<div class="details-row"><span class="meta">Создан</span>{html.escape(ticket.created_at)}</div>'
        f'<div class="details-row"><span class="meta">Обновлен</span>{html.escape(ticket.updated_at)}</div>'
        "</div></details>"
    )
