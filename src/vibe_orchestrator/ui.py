from __future__ import annotations

import html
import json
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from .config import load_all_workflows
from .control import WorkerControl
from .git_trees import GitTreeManager
from .tickets import TicketStore, automatic_retry_available, next_status_for_ticket, reset_failed_retry, retry_exhausted

CSS = """:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#e6edf3;background:#0d1117}body{margin:0}header{display:flex;flex-wrap:wrap;gap:18px;align-items:center;padding:14px 18px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117;z-index:2}header form{display:flex;gap:7px;align-items:center}header button{margin-top:0}input,select{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:6px}input[type=number]{width:52px}.create-form{display:flex;flex-wrap:wrap;gap:7px;align-items:center;padding:12px 14px;border-bottom:1px solid #30363d}.create-form input[name=title]{min-width:220px}.create-form input[name=description]{min-width:220px}a{color:#58a6ff;text-decoration:none}.board{display:flex;gap:12px;padding:14px;align-items:flex-start;overflow-x:auto;min-height:calc(100vh - 72px)}.column{width:260px;min-width:260px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}.column h3{font-size:13px;margin:0 0 10px;color:#8b949e;text-transform:uppercase}.card{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:10px;margin-bottom:9px}.card strong{display:block;font-size:14px;margin:4px 0}.meta{color:#8b949e;font-size:12px}.badge{display:inline-block;border:1px solid #30363d;border-radius:999px;padding:2px 6px;font-size:11px;margin-right:4px}button{background:#238636;color:white;border:0;border-radius:6px;padding:6px 8px;cursor:pointer;margin-top:8px}.summary{margin-top:7px;color:#c9d1d9;font-size:12px;white-space:pre-wrap}.details{margin-top:8px;border-top:1px solid #30363d;padding-top:8px}.details summary{cursor:pointer;color:#58a6ff;font-size:12px}.details-body{margin-top:8px;display:grid;gap:6px}.details-row{font-size:12px;color:#c9d1d9;white-space:pre-wrap}.details-row .meta{display:block;margin-bottom:2px}"""
AUTO_REFRESH_SECONDS = 5
AUTO_REFRESH_SCRIPT = f"""<script>
setInterval(() => {{
  if (document.hidden) return;
  if (document.querySelector('details[open]')) return;
  if (document.activeElement && document.activeElement.matches('input, select, textarea')) return;
  window.location.reload();
}}, {AUTO_REFRESH_SECONDS * 1000});
</script>"""


def _build_server(project: Path, host: str, port: int) -> ThreadingHTTPServer:
    store = TicketStore(project); store.init(); workflows = load_all_workflows(); worker_control = WorkerControl(project); tree_manager = GitTreeManager(project, store)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                query = urllib.parse.parse_qs(parsed.query); process = query.get("process", ["discovery"])[0]
                return self._html(render_board(store, workflows, process, worker_control, tree_manager))
            if parsed.path == "/api/tickets": return self._json([ticket.to_dict() for ticket in store.list()])
            self.send_error(404)
        def do_POST(self):
            length = int(self.headers.get("content-length", "0")); data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
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


def render_board(store, workflows, process: str, worker_control: WorkerControl | None = None, tree_manager: GitTreeManager | None = None) -> str:
    workflow=workflows.get(process) or workflows["discovery"]; tickets=store.list(workflow.id)
    nav=" ".join(f'<a href="/?process={p.id}">{html.escape(p.title)}</a>' for p in workflows.values()); columns=[]
    for stage in workflow.stages:
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
            blocked=f'<span class="badge">заблокирован: {len(ticket.blocked_by)}</span>' if ticket.blocked_by else ""; run='<span class="badge">агент выполняется</span>' if ticket.active_run else ""; retry='<span class="badge">ожидает автоповтора</span>' if stage.kind == "agent" and automatic_retry_available(ticket) and not ticket.active_run else ""; corrective='<span class="badge">без учета WIP</span>' if ticket.wip_exempt else ""; summary=f'<div class="summary">{html.escape(ticket.last_summary or "")}</div>' if ticket.last_summary else ""; tree=tree_manager.trees.get(ticket.id) if tree_manager else None; details=_ticket_details_html(ticket, tree)
            cards.append(f'<div class="card"><span class="meta">{html.escape(ticket.id)}</span><strong>{html.escape(ticket.title)}</strong><span class="badge">{html.escape(ticket.type)}</span>{corrective}{blocked}{run}{retry}<div class="meta">приоритет {ticket.priority}</div>{summary}{details}{action}</div>')
        wip=f" · WIP {stage.wip}" if stage.wip is not None else ""; columns.append(f'<section class="column"><h3>{html.escape(stage.title)}{wip}</h3>{"".join(cards)}</section>')
    refresh_hint = f"автообновление {AUTO_REFRESH_SECONDS}с, пауза при открытых деталях"
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
        '<input name="description" placeholder="описание">'
        '<input name="priority" type="number" min="0" value="100" title="Приоритет">'
        '<input name="parent" placeholder="родительский ID, необязательно">'
        '<button>Создать</button></form>'
    )
    return f'<!doctype html><html><head><meta charset="utf-8"><title>vibe · {html.escape(workflow.title)}</title><style>{CSS}</style>{AUTO_REFRESH_SCRIPT}</head><body><header><strong>vibe-orchestrator</strong>{nav}{worker_form}<span class="meta">{html.escape(str(store.project))}</span><span class="meta">{html.escape(refresh_hint)}</span></header>{create_form}<main class="board">{"".join(columns)}</main></body></html>'


def _ticket_details_html(ticket, tree=None) -> str:
    parent = ticket.parent or "нет"
    blockers = ", ".join(ticket.blocked_by) if ticket.blocked_by else "нет"
    outcome = ticket.last_outcome or "нет"
    retry_after = ticket.retry_after or "нет"
    description = ticket.description or "(пусто)"
    tree_details = ""
    if tree:
        tree_details = (
            f'<div class="details-row"><span class="meta">Ветка</span>{html.escape(tree.branch)}</div>'
            f'<div class="details-row"><span class="meta">Worktree</span>{html.escape(tree.worktree)}</div>'
            f'<div class="details-row"><span class="meta">Интеграция</span>{html.escape(tree.integration_status)}</div>'
        )
    return (
        '<details class="details"><summary>Подробнее</summary><div class="details-body">'
        f'<div class="details-row"><span class="meta">Описание</span>{html.escape(description)}</div>'
        f'<div class="details-row"><span class="meta">Родитель</span>{html.escape(parent)}</div>'
        f'<div class="details-row"><span class="meta">Блокирует</span>{html.escape(blockers)}</div>'
        f'<div class="details-row"><span class="meta">Последний outcome</span>{html.escape(outcome)}</div>'
        f'<div class="details-row"><span class="meta">Ошибок подряд</span>{ticket.consecutive_failures}</div>'
        f'<div class="details-row"><span class="meta">Повтор после</span>{html.escape(retry_after)}</div>'
        f'{tree_details}'
        f'<div class="details-row"><span class="meta">Создан</span>{html.escape(ticket.created_at)}</div>'
        f'<div class="details-row"><span class="meta">Обновлен</span>{html.escape(ticket.updated_at)}</div>'
        "</div></details>"
    )
