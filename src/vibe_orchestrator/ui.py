from __future__ import annotations

import html
import json
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import load_all_workflows
from .tickets import TicketStore, automatic_retry_available, next_status_for_ticket, reset_failed_retry, retry_exhausted

CSS = """:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#e6edf3;background:#0d1117}body{margin:0}header{display:flex;gap:18px;align-items:center;padding:14px 18px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117;z-index:2}a{color:#58a6ff;text-decoration:none}.board{display:flex;gap:12px;padding:14px;align-items:flex-start;overflow-x:auto;min-height:calc(100vh - 72px)}.column{width:260px;min-width:260px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}.column h3{font-size:13px;margin:0 0 10px;color:#8b949e;text-transform:uppercase}.card{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:10px;margin-bottom:9px}.card strong{display:block;font-size:14px;margin:4px 0}.meta{color:#8b949e;font-size:12px}.badge{display:inline-block;border:1px solid #30363d;border-radius:999px;padding:2px 6px;font-size:11px;margin-right:4px}button{background:#238636;color:white;border:0;border-radius:6px;padding:6px 8px;cursor:pointer;margin-top:8px}.summary{margin-top:7px;color:#c9d1d9;font-size:12px;white-space:pre-wrap}.details{margin-top:8px;border-top:1px solid #30363d;padding-top:8px}.details summary{cursor:pointer;color:#58a6ff;font-size:12px}.details-body{margin-top:8px;display:grid;gap:6px}.details-row{font-size:12px;color:#c9d1d9;white-space:pre-wrap}.details-row .meta{display:block;margin-bottom:2px}"""
AUTO_REFRESH_SECONDS = 5
AUTO_REFRESH_SCRIPT = f"""<script>
setInterval(() => {{
  if (document.hidden) return;
  if (document.querySelector('details[open]')) return;
  window.location.reload();
}}, {AUTO_REFRESH_SECONDS * 1000});
</script>"""


def serve(project: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    store = TicketStore(project); store.init(); workflows = load_all_workflows()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                query = urllib.parse.parse_qs(parsed.query); process = query.get("process", ["discovery"])[0]
                return self._html(render_board(store, workflows, process))
            if parsed.path == "/api/tickets": return self._json([ticket.to_dict() for ticket in store.list()])
            self.send_error(404)
        def do_POST(self):
            length = int(self.headers.get("content-length", "0")); data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            if self.path == "/move":
                ticket = store.get(data["id"][0]); workflow = workflows[ticket.process]; target = next_status_for_ticket(store, ticket); requested = data.get("target", [target])[0]
                if not target or requested != target or target not in workflow.by_id: return self.send_error(400, "Переход недоступен")
                ticket.status = target; store.save(ticket); return self._redirect(f"/?process={ticket.process}")
            if self.path == "/retry":
                ticket = store.get(data["id"][0])
                if not reset_failed_retry(ticket): return self.send_error(400, "Повтор недоступен")
                store.save(ticket); return self._redirect(f"/?process={ticket.process}")
            self.send_error(404)
        def log_message(self, fmt, *args): return
        def _html(self, text):
            payload=text.encode("utf-8"); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def _json(self,obj):
            payload=json.dumps(obj,ensure_ascii=False,indent=2).encode("utf-8"); self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def _redirect(self,location): self.send_response(303); self.send_header("Location",location); self.end_headers()
    server=ThreadingHTTPServer((host,port),Handler); url=f"http://{host}:{port}"; print(f"Интерфейс: {url}")
    if open_browser: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass


def render_board(store, workflows, process: str) -> str:
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
            blocked=f'<span class="badge">заблокирован: {len(ticket.blocked_by)}</span>' if ticket.blocked_by else ""; run='<span class="badge">агент выполняется</span>' if ticket.active_run else ""; retry='<span class="badge">ожидает автоповтора</span>' if stage.kind == "agent" and automatic_retry_available(ticket) and not ticket.active_run else ""; corrective='<span class="badge">без учета WIP</span>' if ticket.wip_exempt else ""; summary=f'<div class="summary">{html.escape(ticket.last_summary or "")}</div>' if ticket.last_summary else ""; details=_ticket_details_html(ticket)
            cards.append(f'<div class="card"><span class="meta">{html.escape(ticket.id)}</span><strong>{html.escape(ticket.title)}</strong><span class="badge">{html.escape(ticket.type)}</span>{corrective}{blocked}{run}{retry}<div class="meta">приоритет {ticket.priority}</div>{summary}{details}{action}</div>')
        wip=f" · WIP {stage.wip}" if stage.wip is not None else ""; columns.append(f'<section class="column"><h3>{html.escape(stage.title)}{wip}</h3>{"".join(cards)}</section>')
    refresh_hint = f"автообновление {AUTO_REFRESH_SECONDS}с, пауза при открытых деталях"
    return f'<!doctype html><html><head><meta charset="utf-8"><title>vibe · {html.escape(workflow.title)}</title><style>{CSS}</style>{AUTO_REFRESH_SCRIPT}</head><body><header><strong>vibe-orchestrator</strong>{nav}<span class="meta">{html.escape(str(store.project))}</span><span class="meta">{html.escape(refresh_hint)}</span></header><main class="board">{"".join(columns)}</main></body></html>'


def _ticket_details_html(ticket) -> str:
    parent = ticket.parent or "нет"
    blockers = ", ".join(ticket.blocked_by) if ticket.blocked_by else "нет"
    outcome = ticket.last_outcome or "нет"
    retry_after = ticket.retry_after or "нет"
    description = ticket.description or "(пусто)"
    return (
        '<details class="details"><summary>Подробнее</summary><div class="details-body">'
        f'<div class="details-row"><span class="meta">Описание</span>{html.escape(description)}</div>'
        f'<div class="details-row"><span class="meta">Родитель</span>{html.escape(parent)}</div>'
        f'<div class="details-row"><span class="meta">Блокирует</span>{html.escape(blockers)}</div>'
        f'<div class="details-row"><span class="meta">Последний outcome</span>{html.escape(outcome)}</div>'
        f'<div class="details-row"><span class="meta">Ошибок подряд</span>{ticket.consecutive_failures}</div>'
        f'<div class="details-row"><span class="meta">Повтор после</span>{html.escape(retry_after)}</div>'
        f'<div class="details-row"><span class="meta">Создан</span>{html.escape(ticket.created_at)}</div>'
        f'<div class="details-row"><span class="meta">Обновлен</span>{html.escape(ticket.updated_at)}</div>'
        "</div></details>"
    )
