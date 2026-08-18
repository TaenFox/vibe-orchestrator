from __future__ import annotations

import html
import json
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from .config import load_all_workflows
from .control import DeliverySessionStore, SessionError, WorkerControl
from .budget_ledger import BudgetLedger
from .git_trees import GitTreeManager
from .tickets import (
    TICKET_TYPES_BY_PROCESS,
    TicketStore,
    automatic_retry_available,
    next_status_for_ticket,
    reset_failed_retry,
    retry_exhausted,
)
from .token_usage import is_confirmed_token_usage, unknown_token_usage


@dataclass
class BudgetReadContext:
    """Request/render-local budget snapshots; never shared between responses."""

    snapshots: dict[str, tuple[dict | None, list[dict]]] = field(default_factory=dict)

# Merge retained the drawer implementation from the ticket and the current main UI helpers.
CSS = """:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#e6edf3;background:#0d1117}*{box-sizing:border-box}body{margin:0;overflow-x:hidden}body.drawer-open{overflow:hidden}header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 18px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117;z-index:2}header form{display:flex;gap:7px;align-items:center;flex-wrap:wrap}header button{margin-top:0}input,select,textarea{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:6px;max-width:100%;font:inherit}input[type=number]{width:52px}.create-form{display:flex;flex-wrap:wrap;gap:7px;align-items:center;padding:12px 14px;border-bottom:1px solid #30363d}.create-form input[name=title],.create-form textarea[name=description]{min-width:220px}.create-form textarea{min-height:34px;resize:vertical}a{color:#58a6ff;text-decoration:none}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid #f0c674;outline-offset:2px}.board-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #30363d}.board{display:flex;gap:12px;padding:14px;align-items:flex-start;overflow-x:auto;min-height:calc(100vh - 120px)}.compact-group{display:flex;flex-direction:column;gap:8px;width:260px;min-width:260px}.compact-group>.column{width:100%;min-width:0}.column{width:260px;min-width:260px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}.column h3{font-size:13px;margin:0 0 10px;color:#8b949e;text-transform:uppercase}.card{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:10px;margin-bottom:9px}.card strong{display:block;font-size:14px;margin:4px 0;overflow-wrap:anywhere}.card.active-run{border-color:#f0c674;box-shadow:0 0 0 1px #f0c67444}.meta{color:#8b949e;font-size:12px}.badge{display:inline-block;border:1px solid #30363d;border-radius:999px;padding:2px 6px;font-size:11px;margin-right:4px}.active-badge{color:#f0c674;border-color:#f0c674}button{background:#238636;color:white;border:0;border-radius:6px;padding:6px 8px;cursor:pointer;margin-top:8px}.summary{margin-top:7px;color:#c9d1d9;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}.details{margin-top:8px;border-top:1px solid #30363d;padding-top:8px}.details summary{cursor:pointer;color:#58a6ff;font-size:12px}.details-body{margin-top:8px;display:grid;gap:6px}.details-row{font-size:12px;color:#c9d1d9;white-space:pre-wrap;overflow-wrap:anywhere}.details-row .meta{display:block;margin-bottom:2px}.flat-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));align-items:start;overflow-x:hidden}.flat-list .column{width:auto;min-width:0}.board-empty{padding:24px;color:#8b949e}.drawer-backdrop{position:fixed;inset:0;background:#0008;z-index:10}.ticket-drawer{position:fixed;top:0;right:0;width:min(560px,100vw);height:100vh;overflow-y:auto;background:#161b22;border-left:1px solid #30363d;padding:20px;z-index:11;box-shadow:-12px 0 30px #0008}.drawer-header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.drawer-header h2{margin:0;overflow-wrap:anywhere}.drawer-close{background:#30363d;margin:0}.drawer-section{border-top:1px solid #30363d;margin-top:16px;padding-top:12px}.drawer-section h3{font-size:12px;text-transform:uppercase;color:#8b949e;margin:0 0 8px}.drawer-description{white-space:pre-wrap;overflow-wrap:anywhere}.drawer-actions{display:flex;flex-wrap:wrap;gap:7px}.drawer-actions form{display:inline}.drawer-actions button{margin:0}.run-history{display:grid;gap:8px}.run-entry{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px}.run-entry.active-run{border-color:#f0c674}.run-entry a{display:inline-block;margin-top:4px}@media (max-width:700px){header{gap:10px;padding:10px}header form,.create-form,.board-toolbar{width:100%}.create-form input,.create-form select,.create-form textarea{flex:1 1 100%;min-width:0}.board{padding:10px;gap:8px}.compact-group{width:min(260px,calc(100vw - 20px));min-width:min(260px,calc(100vw - 20px))}.column{width:min(260px,calc(100vw - 20px));min-width:min(260px,calc(100vw - 20px))}}"""
# Current main UI styling follows.
CSS = """:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#e6edf3;background:#0d1117}*{box-sizing:border-box}body{margin:0;overflow-x:hidden}header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 18px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117;z-index:2}header form{display:flex;gap:7px;align-items:center;flex-wrap:wrap}header button{margin-top:0}input,select,textarea{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:5px;padding:6px;max-width:100%;font:inherit}input[type=number]{width:72px}.create-form{display:grid;gap:10px}.create-form input[name=title],.create-form textarea[name=description]{min-width:220px}.create-form textarea{min-height:90px;resize:vertical}.create-dialog{width:min(560px,calc(100vw - 24px));color:#e6edf3;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px}.create-dialog::backdrop{background:rgb(0 0 0 / 65%)}.create-dialog h2{margin:0}.create-dialog .form-actions{display:flex;gap:8px;justify-content:flex-end}.create-dialog label{display:grid;gap:4px}.create-dialog .hint{color:#8b949e;font-size:12px}a{color:#58a6ff;text-decoration:none}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid #f0c674;outline-offset:2px}.board-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #30363d}.board{display:flex;gap:12px;padding:14px;align-items:flex-start;overflow-x:auto;min-height:calc(100vh - 120px)}.compact-group{display:flex;flex-direction:column;gap:8px;width:260px;min-width:260px}.compact-group>.column{width:100%;min-width:0}.column{width:260px;min-width:260px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}.column h3{font-size:13px;margin:0 0 10px;color:#8b949e;text-transform:uppercase}.card{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:10px;margin-bottom:9px}.card strong{display:block;font-size:14px;margin:4px 0;overflow-wrap:anywhere}.meta{color:#8b949e;font-size:12px}.badge{display:inline-block;border:1px solid #30363d;border-radius:999px;padding:2px 6px;font-size:11px;margin-right:4px}button{background:#238636;color:white;border:0;border-radius:6px;padding:6px 8px;cursor:pointer;margin-top:8px}.summary{margin-top:7px;color:#c9d1d9;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}.details{margin-top:8px;border-top:1px solid #30363d;padding-top:8px}.details summary{cursor:pointer;color:#58a6ff;font-size:12px}.details-body{margin-top:8px;display:grid;gap:6px}.details-row{font-size:12px;color:#c9d1d9;white-space:pre-wrap;overflow-wrap:anywhere}.details-row .meta{display:block;margin-bottom:2px}.flat-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));align-items:start;overflow-x:hidden}.flat-list .column{width:auto;min-width:0}.board-empty{padding:24px;color:#8b949e}@media (max-width:700px){header{gap:10px;padding:10px}header form,.board-toolbar{width:100%}.create-dialog{padding:14px}.board{padding:10px;gap:8px}.compact-group{width:min(260px,calc(100vw - 20px));min-width:min(260px,calc(100vw - 20px))}.column{width:min(260px,calc(100vw - 20px));min-width:min(260px,calc(100vw - 20px))}}"""
# End merged UI styling.
CSS += ".drawer-backdrop{position:fixed;inset:0;background:#0008;z-index:10}.ticket-drawer{position:fixed;top:0;right:0;width:min(560px,100vw);height:100vh;overflow-y:auto;background:#161b22;border-left:1px solid #30363d;padding:20px;z-index:11;box-shadow:-12px 0 30px #0008}.drawer-header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.drawer-header h2{margin:0;overflow-wrap:anywhere}.drawer-close{background:#30363d;margin:0}.drawer-section{border-top:1px solid #30363d;margin-top:16px;padding-top:12px}.drawer-section h3{font-size:12px;text-transform:uppercase;color:#8b949e;margin:0 0 8px}.drawer-description{white-space:pre-wrap;overflow-wrap:anywhere}.drawer-actions{display:flex;flex-wrap:wrap;gap:7px}.drawer-actions form{display:inline}.drawer-actions button{margin:0}.run-history{display:grid;gap:8px}.run-entry{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px}.run-entry.active-run{border-color:#f0c674}.run-entry a{display:inline-block;margin-top:4px}"
AUTO_REFRESH_SECONDS = 8
AUTO_REFRESH_SCRIPT = f"""<script>
(() => {{
  const key = 'vibe-board-state';
  const storage = {{ get: () => {{ try {{ return sessionStorage.getItem(key) || '{{}}'; }} catch (_) {{ return '{{}}'; }} }}, set: value => {{ try {{ sessionStorage.setItem(key, value); }} catch (_) {{}} }} }};
  const state = () => {{ try {{ return JSON.parse(storage.get()); }} catch (_) {{ return {{}}; }} }};
  const save = (extra = {{}}) => storage.set(JSON.stringify({{...state(), process: new URLSearchParams(location.search).get('process') || 'discovery', ...extra}}));
  const controls = () => [...document.querySelectorAll('input,select,textarea')];
  const controlKey = el => el.dataset.boardStateKey || `${{el.form?.getAttribute('action') || ''}}:${{el.name || el.type || el.tagName.toLowerCase()}}`;
  const detailsState = () => [...document.querySelectorAll('details[data-ticket-details]')].map(el => [el.dataset.ticketDetails, el.open]);
  const remember = () => {{
    const active = document.activeElement; const board = document.querySelector('.board');
    const inputs = controls().map(el => [controlKey(el), el.value]);
    save({{mode: document.querySelector('[data-board-mode]')?.value || 'compact', ticket: document.querySelector('.card[data-ticket].selected')?.dataset.ticket || state().ticket, boardScrollX: board?.scrollLeft || 0, scrollY: window.scrollY || document.scrollingElement?.scrollTop || 0, inputs, details: detailsState(), focus: active ? controls().indexOf(active) : -1}});
  }};
  const restore = () => {{
    const restored = state(); const savedInputs = new Map(restored.inputs || []); const savedDetails = new Map(restored.details || []);
    controls().forEach(el => {{ const value = savedInputs.get(controlKey(el)); if (value !== undefined && !el.matches(':focus')) el.value = value; }});
    document.querySelectorAll('details[data-ticket-details]').forEach(el => {{ if (savedDetails.has(el.dataset.ticketDetails)) el.open = savedDetails.get(el.dataset.ticketDetails); }});
  }};
  let inFlight = false; let lastInteraction = 0;
  const toggle = () => document.querySelector('[data-refresh-toggle]');
  const updateToggle = () => {{ const paused = state().refreshPaused === true; const button = toggle(); if (button) {{ button.textContent = paused ? 'Обновление: пауза' : 'Обновление: включено'; button.setAttribute('aria-pressed', String(paused)); }} }};
  const busy = force => {{
    if (force) return false;
    const drawer = document.querySelector('[data-ticket-drawer]');
    return document.hidden || Date.now() - lastInteraction < 1200 || drawer && !drawer.hidden || document.querySelector('details[open]') || document.activeElement?.matches('input, select, textarea');
  }};
  const refresh = async (force = false) => {{
    if (inFlight || state().refreshPaused === true || busy(force)) return;
    inFlight = true; remember(); const current = state();
    const params = new URLSearchParams({{process: current.process || 'discovery', mode: current.mode || 'compact', search: current.search || '', status: current.status || '', active: current.active ? '1' : ''}});
    try {{
      const response = await fetch('/fragment?' + params, {{cache: 'no-store'}}); if (!response.ok) return;
      const fragment = await response.text(); const board = document.querySelector('.board');
      if (!board || fragment === board.outerHTML) return;
      board.outerHTML = fragment; restore(); const restored = state();
      if (restored.ticket) document.querySelector(`.card[data-ticket="${{CSS.escape(restored.ticket)}}"]`)?.classList.add('selected');
      const refreshedBoard = document.querySelector('.board'); if (refreshedBoard && restored.boardScrollX != null) refreshedBoard.scrollLeft = restored.boardScrollX;
      if (restored.scrollY != null) window.scrollTo(0, restored.scrollY); if (restored.focus >= 0) controls()[restored.focus]?.focus();
    }} catch (_) {{ /* transient server/network failure: keep the current board */ }} finally {{ inFlight = false; }}
  }};
  let ticketCreationInProgress = false;
  document.addEventListener('submit', event => {{
    const form = event.target.closest?.('form[action="/create"]'); if (!form || ticketCreationInProgress) return;
    event.preventDefault(); ticketCreationInProgress = true; lastInteraction = Date.now(); const submit = form.querySelector('button[type="submit"]'); if (submit) submit.disabled = true;
    const dialog = form.closest('dialog'); dialog?.close(); const process = form.elements.process?.value || 'discovery'; save({{process}});
    fetch('/create', {{method: 'POST', body: new URLSearchParams(new FormData(form))}}).then(response => {{ if (!response.ok) throw new Error('Не удалось создать тикет'); form.reset(); return refresh(true); }}).catch(() => dialog?.showModal()).finally(() => {{ ticketCreationInProgress = false; if (submit) submit.disabled = false; }});
  }});
  document.addEventListener('input', event => {{ if (event.target.matches('input,select,textarea')) {{ lastInteraction = Date.now(); remember(); }} }});
  document.addEventListener('change', event => {{ if (event.target.matches('[data-board-mode], [data-board-search], [data-board-status], [data-board-active]')) {{ lastInteraction = 0; const value = event.target.value; save(event.target.matches('[data-board-mode]') ? {{mode:value}} : event.target.matches('[data-board-search]') ? {{search:value}} : event.target.matches('[data-board-status]') ? {{status:value}} : {{active:value === '1'}}); refresh(true); }} }});
  document.addEventListener('toggle', event => {{ if (event.target.matches('details[data-ticket-details]')) {{ lastInteraction = Date.now(); remember(); }} }}, true);
  document.addEventListener('click', event => {{
    lastInteraction = Date.now(); const link = event.target.closest('a[href*="?process="]'); if (link) {{ remember(); save({{process: new URL(link.href, location.href).searchParams.get('process')}}); }}
    const toggleButton = event.target.closest('[data-refresh-toggle]'); if (toggleButton) {{ save({{refreshPaused: state().refreshPaused !== true}}); updateToggle(); return; }}
    const open = event.target.closest('[data-open-ticket]'); if (open) {{ event.preventDefault(); const drawer = document.querySelector('[data-ticket-drawer]'); const backdrop = document.querySelector('[data-drawer-backdrop]'); const ticketId = open.dataset.openTicket; const panel = drawer?.querySelector(`[data-drawer-ticket="${{CSS.escape(ticketId)}}"]`); if (!drawer || !panel) return; drawer.querySelectorAll('[data-drawer-ticket]').forEach(item => item.hidden = item !== panel); panel.hidden = false; drawer.hidden = false; if (backdrop) backdrop.hidden = false; drawer.setAttribute('aria-hidden', 'false'); drawer.dataset.previousFocus = open.id || ''; document.body.classList.add('drawer-open'); panel.focus(); save({{ticket: ticketId}}); fetch('/drawer?' + new URLSearchParams({{process: state().process || 'discovery', ticket: ticketId}}), {{cache: 'no-store'}}).then(response => response.ok ? response.text() : '').then(text => {{ if (!text) return; const updated = document.createRange().createContextualFragment(text).firstElementChild; if (updated) {{ updated.hidden = false; panel.replaceWith(updated); updated.focus(); }} }}).catch(() => {{}}); return; }}
    if (event.target.matches('[data-drawer-close], [data-drawer-backdrop]')) {{ const drawer = document.querySelector('[data-ticket-drawer]'); const backdrop = document.querySelector('[data-drawer-backdrop]'); if (drawer) {{ drawer.hidden = true; drawer.setAttribute('aria-hidden', 'true'); drawer.querySelectorAll('[data-drawer-ticket]').forEach(panel => panel.hidden = true); }} if (backdrop) backdrop.hidden = true; document.body.classList.remove('drawer-open'); refresh(true); }}
    const card = event.target.closest('.card[data-ticket]'); if (card) {{ document.querySelectorAll('.card.selected').forEach(item => item.classList.remove('selected')); card.classList.add('selected'); save({{ticket: card.dataset.ticket}}); }}
  }});
  document.addEventListener('keydown', event => {{
    const drawer = document.querySelector('[data-ticket-drawer]'); const panel = drawer?.querySelector('[data-drawer-ticket]:not([hidden])');
    if (!drawer || drawer.hidden || !panel) return;
    if (event.key === 'Escape') {{ const close = document.querySelector('[data-drawer-close]'); if (close) close.click(); return; }}
    if (event.key !== 'Tab') return;
    const focusable = [...panel.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(item => !item.hidden && item.getAttribute('aria-hidden') !== 'true');
    if (!focusable.length) {{ event.preventDefault(); panel.focus(); return; }}
    const first = focusable[0]; const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {{ event.preventDefault(); last.focus(); }}
    else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {{ event.preventDefault(); first.focus(); }}
  }});
  const current = state(); const modeControl = document.querySelector('[data-board-mode]'); const searchControl = document.querySelector('[data-board-search]'); const statusControl = document.querySelector('[data-board-status]'); const activeControl = document.querySelector('[data-board-active]');
  if (modeControl && current.mode) modeControl.value = current.mode; if (searchControl && current.search) searchControl.value = current.search; if (statusControl && current.status) statusControl.value = current.status; if (activeControl && current.active) activeControl.value = '1'; restore(); updateToggle();
  if (current.ticket) {{ const selected = document.querySelector(`.card[data-ticket="${{CSS.escape(current.ticket)}}"]`); if (selected) selected.classList.add('selected'); }} const initialBoard = document.querySelector('.board'); if (initialBoard && current.boardScrollX != null) initialBoard.scrollLeft = current.boardScrollX; if (current.scrollY != null) window.scrollTo(0, current.scrollY); setInterval(refresh, {AUTO_REFRESH_SECONDS * 1000});
}})();
</script>"""


def _build_server(project: Path, host: str, port: int) -> ThreadingHTTPServer:
    store = TicketStore(project); store.init(); workflows = load_all_workflows(); worker_control = WorkerControl(project); tree_manager = GitTreeManager(project, store); session_store = DeliverySessionStore(project); ledger = BudgetLedger(project)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                query = urllib.parse.parse_qs(parsed.query); process = query.get("process", ["discovery"])[0]
                mode = query.get("mode", ["compact"])[0]; search = query.get("search", [""])[0]; status = query.get("status", [""])[0]; active = query.get("active", [""])[0]
                return self._html(render_board(store, workflows, process, worker_control, tree_manager, session_store, mode=mode, search=search, status=status, active=active))
            if parsed.path == "/fragment":
                query = urllib.parse.parse_qs(parsed.query)
                return self._html(render_board_fragment(store, workflows, query.get("process", ["discovery"])[0], worker_control, tree_manager, session_store, mode=query.get("mode", ["compact"])[0], search=query.get("search", [""])[0], status=query.get("status", [""])[0], active=query.get("active", [""])[0]))
            if parsed.path == "/drawer":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    ticket = store.get(query.get("ticket", [""])[0])
                except KeyError:
                    return self.send_error(404)
                return self._html(_ticket_drawer_panel_html(store, workflows, query.get("process", ["discovery"])[0], ticket, tree_manager, session_store, ledger, BudgetReadContext()))
            if parsed.path.startswith("/artifacts/"):
                relative = urllib.parse.unquote(parsed.path.removeprefix("/artifacts/")).strip("/")
                candidate = (store.runs_root / relative).resolve()
                if store.runs_root.resolve() not in candidate.parents and candidate != store.runs_root.resolve():
                    return self.send_error(404)
                if candidate.is_dir():
                    files = [path.name for path in sorted(candidate.iterdir()) if path.is_file()]
                    return self._json({"run_id": candidate.name, "artifacts": [f"/artifacts/{urllib.parse.quote(relative + '/' + name)}" for name in files]})
                if candidate.is_file():
                    payload = candidate.read_bytes(); self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
                return self.send_error(404)
            if parsed.path == "/api/tickets":
                context = BudgetReadContext()
                return self._json([_ticket_payload(ticket, ledger, context) for ticket in store.list()])
            if parsed.path == "/api/sessions":
                context = BudgetReadContext()
                return self._json([_session_payload(item, store, ledger, context) for item in session_store.list()])
            if parsed.path.startswith("/api/sessions/"):
                try:
                    return self._json(_session_payload(session_store.get(parsed.path.rsplit("/", 1)[-1]), store, ledger, BudgetReadContext()))
                except SessionError as exc:
                    return self.send_error(404, str(exc))
            self.send_error(404)
        def do_POST(self):
            length = int(self.headers.get("content-length", "0")); data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            try:
                if self.path == "/session/create":
                    session_store.create(data.get("title", [""])[0]); return self._redirect("/?process=delivery")
                if self.path == "/session/add":
                    session_id = data.get("session", [""])[0]
                    ticket_id = data.get("ticket", [""])[0]
                    if not session_id or not ticket_id:
                        return self.send_error(400, "Тикет для добавления не выбран")
                    session_store.add(session_id, ticket_id, store); return self._redirect("/?process=delivery")
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
                    priority_raw = data.get("priority", ["100"])[0].strip()
                    priority = int(priority_raw)
                    parent = data.get("parent", [""])[0].strip() or None
                    _validate_ticket_creation(store, workflows, process, ticket_type, title, priority, parent)
                    ticket = store.create(process, ticket_type, title, description=description, priority=priority, parent=parent)
                except (KeyError, ValueError) as exc:
                    log_message = str(exc) or "Некорректные данные тикета"
                    return self.send_error(400, log_message)
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
            payload=text.encode("utf-8"); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(payload))); self.end_headers()
            try: self.wfile.write(payload)
            except BrokenPipeError: pass
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


# Main branch ticket-creation helpers are preserved above the renderer.
def _parent_is_compatible(parent, process: str, ticket_type: str) -> bool:
    if process == "discovery":
        return ticket_type == "correction" and parent.process == "discovery" and parent.type == "idea"
    if process == "delivery":
        if ticket_type == "rework":
            return parent.process == "delivery" and parent.type != "rework"
        return (parent.process == "discovery" and parent.type == "idea") or (parent.process == "delivery" and parent.type != "rework")
    return False


def _validate_ticket_creation(store, workflows, process: str, ticket_type: str, title: str, priority: int, parent: str | None) -> None:
    if process not in workflows or process not in TICKET_TYPES_BY_PROCESS:
        raise ValueError("Неизвестный процесс")
    if ticket_type not in TICKET_TYPES_BY_PROCESS[process]:
        raise ValueError("Недопустимый тип тикета для процесса")
    if not title:
        raise ValueError("Заголовок обязателен")
    if priority < 0:
        raise ValueError("Приоритет должен быть неотрицательным")
    if not parent:
        if ticket_type in {"correction", "rework"}:
            raise ValueError("Для корректирующего тикета нужен parent")
        return
    try:
        parent_ticket = store.get(parent)
    except KeyError as exc:
        raise ValueError("Parent тикет не найден") from exc
    if not _parent_is_compatible(parent_ticket, process, ticket_type):
        raise ValueError("Parent несовместим с процессом и типом тикета")


def _create_ticket_form(store, process: str, workflows) -> str:
    process_options = "".join(
        f'<option value="{html.escape(item.id)}"{(" selected" if item.id == process else "")}>{html.escape(item.title)}</option>'
        for item in workflows.values()
    )
    type_options = "".join(
        f'<option value="{html.escape(ticket_type)}" data-process="{html.escape(item.id)}"{(" selected" if item.id == process and ticket_type == TICKET_TYPES_BY_PROCESS[item.id][0] else "")}>{html.escape(ticket_type)}</option>'
        for item in workflows.values()
        for ticket_type in TICKET_TYPES_BY_PROCESS.get(item.id, ())
    )
    parent_options = ['<option value="">Без parent</option>']
    for ticket in store.list():
        if any(_parent_is_compatible(ticket, process, ticket_type) for ticket_type in TICKET_TYPES_BY_PROCESS[process]):
            parent_options.append(f'<option value="{html.escape(ticket.id)}" data-parent-process="{html.escape(ticket.process)}" data-parent-type="{html.escape(ticket.type)}">{html.escape(ticket.id)}</option>')
    return (
        '<button type="button" onclick="document.getElementById(\'create-ticket-dialog\').showModal()">Новый тикет</button>'
        '<dialog id="create-ticket-dialog" class="create-dialog"><form class="create-form" method="post" action="/create">'
        '<h2>Новый тикет</h2><span class="hint">Выберите тип процесса и заполните обязательные поля.</span>'
        f'<label>Процесс<select name="process">{process_options}</select></label>'
        f'<label>Тип<select name="type" required>{type_options}</select></label>'
        '<label>Заголовок<input name="title" required maxlength="500" autocomplete="off"></label>'
        '<label>Описание<textarea name="description" rows="5" placeholder="Контекст, ожидаемый результат и ограничения"></textarea></label>'
        '<label>Приоритет<input name="priority" type="number" min="0" value="100" required></label>'
        f'<label>Parent<select name="parent">{"".join(parent_options)}</select></label>'
        '<div class="form-actions"><button type="button" onclick="this.closest(\'dialog\').close()">Отмена</button><button type="submit">Создать</button></div>'
        '</form></dialog>'
        '<script>(function(){const dialog=document.getElementById("create-ticket-dialog");const form=dialog.querySelector("form");const process=form.elements.process;const type=form.elements.type;const parent=form.elements.parent;const sync=()=>{const selected=type.value;[...type.options].forEach(option=>option.hidden=option.dataset.process!==process.value);if(type.selectedOptions[0]?.hidden)type.value=[...type.options].find(option=>option.dataset.process===process.value)?.value||"";[...parent.options].forEach(option=>{if(!option.value){option.hidden=false;return;}const compatible=(process.value==="discovery"&&selected==="correction"&&option.dataset.parentProcess==="discovery"&&option.dataset.parentType==="idea")||(process.value==="delivery"&&((selected==="rework"&&option.dataset.parentProcess==="delivery"&&option.dataset.parentType!=="rework")||(selected!=="rework"&&((option.dataset.parentProcess==="discovery"&&option.dataset.parentType==="idea")||(option.dataset.parentProcess==="delivery"&&option.dataset.parentType!=="rework")))));option.hidden=!compatible;if(option.hidden&&option.selected)parent.value="";});};process.addEventListener("change",sync);type.addEventListener("change",sync);sync();})();</script>'
    )


def render_board(store, workflows, process: str, worker_control: WorkerControl | None = None, tree_manager: GitTreeManager | None = None, session_store: DeliverySessionStore | None = None, *, mode: str = "compact", search: str = "", status: str = "", active: str | bool = "") -> str:
# End merged renderer signature.
    ledger = BudgetLedger(store.project)
    budget_context = BudgetReadContext()
    workflow=workflows.get(process) or workflows["discovery"]; tickets=store.list(workflow.id)
    mode = mode if mode in {"compact", "flat"} else "compact"
    search = search.strip()
    if search:
        needle = search.casefold(); tickets = [t for t in tickets if needle in f"{t.id} {t.title} {t.description}".casefold()]
    if status and status in workflow.by_id:
        tickets = [t for t in tickets if t.status == status]
    active = active in {True, "1", "true", "yes", "on"}
    if active:
        tickets = [t for t in tickets if t.active_run]
    nav=" ".join(f'<a href="/?process={p.id}">{html.escape(p.title)}</a>' for p in workflows.values())
    columns=[]
    rendered = set()
    for stage in workflow.stages:
        if stage.id in rendered or (mode == "flat" and stage.kind != "queue"):
            continue
        related = [stage]
        if mode == "compact" and stage.kind == "queue" and stage.pull_to and stage.pull_to in workflow.by_id:
            related.insert(0, workflow.by_id[stage.pull_to])
        rendered.update(item.id for item in related)
        group = []
        for item in related:
            group.append(_stage_column(store, workflow, item, tickets, tree_manager, session_store, ledger, budget_context))
        columns.append(f'<div class="compact-group" data-stage-group="{html.escape(stage.id)}">{"".join(group)}</div>' if mode == "compact" else "".join(group))
    if mode == "flat":
        columns = [_stage_column(store, workflow, stage, tickets, tree_manager, session_store, ledger, budget_context) for stage in workflow.stages]
    board_class = "board flat-list" if mode == "flat" else "board"
    toolbar = _board_toolbar(workflow, mode, search, status, active)
    board = f'<main class="{board_class}">{"".join(columns) or "<div class=board-empty>Нет тикетов по текущему фильтру</div>"}</main>'
    refresh_hint = f"частичное автообновление {AUTO_REFRESH_SECONDS}с"
    worker_control = worker_control or WorkerControl(store.project)
    worker_limit = worker_control.get_limit()
    active_workers = sum(1 for ticket in store.list() if ticket.active_run)
    worker_form = f'<form method="post" action="/workers"><input type="hidden" name="process" value="{html.escape(workflow.id)}"><label class="meta">воркеры <input type="number" name="count" min="0" value="{worker_limit}"></label><button>Применить</button><span class="meta">активно {active_workers}</span></form>'
    create_form = _create_ticket_form(store, workflow.id, workflows)
    sessions_html = _sessions_html(store, session_store, ledger, budget_context) if process == "delivery" else ""
    drawer = _ticket_drawer_html(store, workflows, process, tickets, tree_manager, session_store, ledger, budget_context)
    refresh_control = '<button type="button" data-refresh-toggle aria-pressed="false">Обновление: включено</button>'
    return f'<!doctype html><html><head><meta charset="utf-8"><title>vibe · {html.escape(workflow.title)}</title><style>{CSS}</style>{AUTO_REFRESH_SCRIPT}</head><body><header><strong>vibe-orchestrator</strong>{nav}{worker_form}{refresh_control}<span class="meta">{html.escape(str(store.project))}</span><span class="meta">{html.escape(refresh_hint)}</span></header>{create_form}{sessions_html}{toolbar}{board}{drawer}</body></html>'


def render_board_fragment(store, workflows, process: str, worker_control=None, tree_manager=None, session_store=None, *, mode="compact", search="", status="", active="") -> str:
    ledger = BudgetLedger(store.project)
    budget_context = BudgetReadContext()
    workflow = workflows.get(process) or workflows["discovery"]
    tickets = store.list(workflow.id)
    mode = mode if mode in {"compact", "flat"} else "compact"
    needle = search.strip().casefold()
    if needle:
        tickets = [t for t in tickets if needle in f"{t.id} {t.title} {t.description}".casefold()]
    if status and status in workflow.by_id:
        tickets = [t for t in tickets if t.status == status]
    if active in {True, "1", "true", "yes", "on"}:
        tickets = [t for t in tickets if t.active_run]
    stages = workflow.stages
    if mode == "compact":
        columns=[]; rendered=set()
        for stage in stages:
            if stage.id in rendered: continue
            related=[stage]
            if stage.kind == "queue" and stage.pull_to and stage.pull_to in workflow.by_id: related.insert(0, workflow.by_id[stage.pull_to])
            rendered.update(item.id for item in related)
            columns.append(f'<div class="compact-group" data-stage-group="{html.escape(stage.id)}">{"".join(_stage_column(store, workflow, item, tickets, tree_manager, session_store, ledger, budget_context) for item in related)}</div>')
    else:
        columns=[_stage_column(store, workflow, stage, tickets, tree_manager, session_store, ledger, budget_context) for stage in stages]
    return f'<main class="{"board flat-list" if mode == "flat" else "board"}">{"".join(columns) or "<div class=board-empty>Нет тикетов по текущему фильтру</div>"}</main>'


def _board_toolbar(workflow, mode, search, status, active=False):
    options = '<option value="">Все стадии</option>' + ''.join(f'<option value="{html.escape(stage.id)}"{(" selected" if stage.id == status else "")}>{html.escape(stage.title)}</option>' for stage in workflow.stages)
    active_options = '<option value="">Все тикеты</option><option value="1" selected>В работе</option>' if active else '<option value="">Все тикеты</option><option value="1">В работе</option>'
    return f'<section class="board-toolbar"><label>Режим <select data-board-mode><option value="compact"{(" selected" if mode == "compact" else "")}>Компактный</option><option value="flat"{(" selected" if mode == "flat" else "")}>Плоский список</option></select></label><label>Поиск <input data-board-search type="search" value="{html.escape(search)}" placeholder="ID, заголовок или описание"></label><label>Фильтр <select data-board-status>{options}</select></label><label>Активность <select data-board-active>{active_options}</select></label></section>'


def _stage_column(store, workflow, stage, tickets, tree_manager, session_store, ledger=None, budget_context=None):
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
            blocked=f'<span class="badge">заблокирован: {len(ticket.blocked_by)}</span>' if ticket.blocked_by else ""; run='<span class="badge active-badge">агент выполняется</span>' if ticket.active_run else ""; retry='<span class="badge">ожидает автоповтора</span>' if stage.kind == "agent" and automatic_retry_available(ticket) and not ticket.active_run else ""; corrective='<span class="badge">без учета WIP</span>' if ticket.wip_exempt else ""; session_badge=_ticket_session_badge(ticket, session_store); summary=f'<div class="summary">{html.escape(ticket.last_summary or "")}</div>' if ticket.last_summary else ""; tree=tree_manager.trees.get(ticket.id) if tree_manager else None; details=_ticket_details_html(ticket, tree, ledger, budget_context); drawer_button=f'<button type="button" id="open-ticket-{html.escape(ticket.id)}" class="drawer-trigger" data-open-ticket="{html.escape(ticket.id)}" aria-label="Открыть тикет {html.escape(ticket.id)}">Открыть</button>'
            card_class = "card active-run" if ticket.active_run else "card"
            budget, _ = _budget_read_model(ledger, f"ticket:{ticket.id}", budget_context) if ledger else (None, [])
            budget_html = _budget_summary_html(budget)
            cards.append(f'<div class="{card_class}" data-ticket="{html.escape(ticket.id)}"><span class="meta">{html.escape(ticket.id)}</span><strong>{html.escape(ticket.title)}</strong><span class="badge">{html.escape(ticket.type)}</span>{session_badge}{corrective}{blocked}{run}{retry}<div class="meta">приоритет {ticket.priority}</div>{budget_html}{summary}{drawer_button}{details}{action}</div>')
        wip=f" · WIP {stage.wip}" if stage.wip is not None else ""; return f'<section class="column" data-stage="{html.escape(stage.id)}"><h3>{html.escape(stage.title)}{wip}</h3>{"".join(cards)}</section>'


def _dimensions(value) -> dict[str, int | None]:
    return {dimension: value.get(dimension) if isinstance(value, dict) else None for dimension in ("tokens", "points", "runs")}


def _source_confidence(actual: dict | None) -> str:
    if not isinstance(actual, dict) or actual.get("source") == "unknown":
        return "unknown"
    if actual.get("source") == "provider":
        return "confirmed"
    return "degraded"


def _budget_run_snapshot(run: dict, usage: dict | None, confidence: str) -> tuple[str, bool]:
    """Return exactness metadata for one usage read-model item."""
    if (run.get("state") == "finalized" and confidence == "confirmed"
            and is_confirmed_token_usage(usage, run_id=run.get("run_id"))):
        return "fresh", True
    return "stale", False


def _budget_run(run: dict) -> dict:
    usage = run.get("actual") or {}
    unknown = run.get("state") == "unknown"
    actual = {"tokens": None, "points": None, "runs": None} if unknown else (_dimensions(run.get("actual")) if run.get("actual") is not None else {"tokens": None, "points": None, "runs": None})
    confidence = "unknown" if unknown else _source_confidence(usage)
    snapshot_status, enforcement_state_exact = _budget_run_snapshot(run, usage, confidence)
    captured_at = usage.get("captured_at") if isinstance(usage, dict) else None
    return {
        "run_id": run["run_id"], "state": run["state"], "attempt_kind": run["attempt_kind"],
        "ticket_id": run["ticket_id"], "parent_ticket_id": run.get("parent_ticket_id"),
        "ticket_budget_id": run.get("ticket_budget_id"), "session_budget_id": run.get("session_budget_id"),
        "planned": _dimensions(run.get("planned")), "reserved": _dimensions(run.get("reserved")), "actual": actual,
        "source": usage.get("source", "unknown") if isinstance(usage, dict) else "unknown",
        "source_confidence": confidence, "captured_at": captured_at,
        "last_confirmed_snapshot_at": captured_at if confidence == "confirmed" else None,
        "snapshot_status": snapshot_status, "enforcement_state_exact": enforcement_state_exact,
        "usage_ref": usage.get("usage_ref") if isinstance(usage, dict) else None,
        "model": usage.get("model") if isinstance(usage, dict) else None,
        "reasoning_effort": usage.get("reasoning_effort") if isinstance(usage, dict) else None,
        "fallback_policy_version": usage.get("fallback_policy_version") if isinstance(usage, dict) else None,
        "degraded_confidence": usage.get("degraded_confidence") if isinstance(usage, dict) else None,
        "currency": usage.get("currency") if isinstance(usage, dict) else None,
        "normalization_version": usage.get("normalization_version") if isinstance(usage, dict) else None,
        "rate_card_version": usage.get("rate_card_version") if isinstance(usage, dict) else None,
        "cost": usage.get("cost") if isinstance(usage, dict) and not unknown else None,
    }


def _budget_read_model(ledger, budget_id: str | None, context: BudgetReadContext | None = None) -> tuple[dict | None, list[dict]]:
    if not budget_id:
        return None, []
    if context is not None and budget_id in context.snapshots:
        return context.snapshots[budget_id]
    try:
        raw = ledger.read_budget(budget_id)
        if raw is None:
            return None, []
        runs = [_budget_run(run) for run in ledger.list_runs(budget_id)]
        started = sum(run["state"] == "started" for run in runs)
        reserved = sum(run["state"] in {"reserved_pending_start", "started"} for run in runs)
        budget = {
            "contract_version": "budget.read.v1", "budget_id": raw["budget_id"], "scope": raw["scope"],
            "owner_id": raw["owner_id"], "mode": raw["mode"],
            "base_limits": {d: raw.get(f"base_limit_{d}") for d in ("tokens", "points", "runs")},
            "limits": raw["limits"], "spent": raw["aggregates"]["finalized"],
            "reserved": raw["aggregates"]["reserved"], "planned": raw["aggregates"]["planned"],
            "available": raw["available"], "started_runs": started, "reserved_runs": reserved,
            "status": raw["status"],
            "blocked_reason": raw["status"] if raw["status"] in {"blocked_unknown", "stop_new_runs", "exhausted", "over_budget", "completed"} else None,
            "observed_at": raw.get("updated_at"), "snapshot_status": raw.get("snapshot_status", "fresh"),
            "enforcement_state_exact": raw.get("enforcement_state_exact", raw.get("snapshot_status", "fresh") == "fresh"),
        }
        result = (budget, runs)
        if context is not None:
            context.snapshots[budget_id] = result
        return result
    except Exception as exc:  # read API fails closed when SQLite is unavailable
        result = ({"contract_version": "budget.read.v1", "budget_id": budget_id, "scope": None, "owner_id": None,
                "mode": None, "base_limits": None, "limits": None, "spent": None, "reserved": None,
                "planned": None, "available": None, "started_runs": 0, "reserved_runs": 0, "status": "unavailable",
                "blocked_reason": None, "observed_at": None, "snapshot_status": "unavailable",
                "enforcement_state_exact": False, "reason": type(exc).__name__}, [])
        if context is not None:
            context.snapshots[budget_id] = result
        return result


def _budget_summary_html(budget: dict | None) -> str:
    if not budget:
        return ""
    if budget.get("snapshot_status") != "fresh":
        label = "budget: stale" if budget.get("snapshot_status") == "stale" else "budget: unavailable"
        return f'<div class="budget-summary"><span class="badge">{label}</span><span class="meta">enforcement state не подтвержден</span></div>'
    limits, spent = budget.get("limits") or {}, budget.get("spent") or {}
    reserved, available = budget.get("reserved") or {}, budget.get("available") or {}
    def fmt(values, dimension):
        return "unlimited" if values.get(dimension) is None else str(values.get(dimension, "—"))
    return (f'<div class="budget-summary"><span class="badge">budget {html.escape(str(budget.get("status")))}</span>'
            f'<span class="meta">tokens: limit {fmt(limits, "tokens")} · spent {fmt(spent, "tokens")} · '
            f'reserved {fmt(reserved, "tokens")} · available {fmt(available, "tokens")} · '
            f'runs {budget.get("started_runs", 0)}/{budget.get("reserved_runs", 0)}</span></div>')


def _budget_value(value) -> str:
    return "—" if value is None else html.escape(str(value))


def _budget_dimensions_html(values: dict | None) -> str:
    values = values or {}
    return " · ".join(f'{dimension}: {_budget_value(values.get(dimension))}' for dimension in ("tokens", "points", "runs"))


def _budget_run_details_html(run: dict) -> str:
    return (
        f'<div class="run-entry">'
        f'<div><span class="badge">{html.escape(str(run.get("state", "—")))}</span> <strong>{html.escape(str(run.get("run_id", "—")))}</strong></div>'
        f'<div class="details-row"><span class="meta">Attempt / ownership</span>{html.escape(str(run.get("attempt_kind") or "—"))} · ticket {html.escape(str(run.get("ticket_id") or "—"))} · parent ticket {html.escape(str(run.get("parent_ticket_id") or "—"))} · ticket budget {html.escape(str(run.get("ticket_budget_id") or "—"))} · session budget {html.escape(str(run.get("session_budget_id") or "—"))}</div>'
        f'<div class="details-row"><span class="meta">Dimensions</span>planned ({_budget_dimensions_html(run.get("planned"))}) · reserved ({_budget_dimensions_html(run.get("reserved"))}) · actual ({_budget_dimensions_html(run.get("actual"))})</div>'
        f'<div class="details-row"><span class="meta">Snapshot</span>snapshot_status {_budget_value(run.get("snapshot_status"))} · enforcement_state_exact {_budget_value(run.get("enforcement_state_exact"))} · source {html.escape(str(run.get("source") or "unknown"))} · confidence {html.escape(str(run.get("source_confidence") or "unknown"))} · captured_at {_budget_value(run.get("captured_at"))} · last_confirmed_snapshot_at {_budget_value(run.get("last_confirmed_snapshot_at"))}</div>'
        f'<div class="details-row"><span class="meta">Versions / cost</span>normalization_version {_budget_value(run.get("normalization_version"))} · rate_card_version {_budget_value(run.get("rate_card_version"))} · cost {_budget_value(run.get("cost"))}</div>'
        '</div>'
    )


def _budget_details_html(budget: dict | None, runs: list[dict] | None = None) -> str:
    if not budget:
        return ""
    limits = budget.get("limits") or {}
    return (
        f'<div class="details-body budget-details">'
        f'<div class="details-row"><span class="meta">Budget identity</span>budget_id {_budget_value(budget.get("budget_id"))} · scope {_budget_value(budget.get("scope"))} · owner_id {_budget_value(budget.get("owner_id"))} · mode {_budget_value(budget.get("mode"))} · contract_version {_budget_value(budget.get("contract_version"))}</div>'
        f'<div class="details-row"><span class="meta">Status</span>status {_budget_value(budget.get("status"))} · blocked_reason {_budget_value(budget.get("blocked_reason"))}</div>'
        f'<div class="details-row"><span class="meta">Dimensions</span>planned ({_budget_dimensions_html(budget.get("planned"))}) · reserved ({_budget_dimensions_html(budget.get("reserved"))}) · actual ({_budget_dimensions_html(budget.get("spent"))}) · available ({_budget_dimensions_html(budget.get("available"))}) · limits ({_budget_dimensions_html(limits)})</div>'
        f'<div class="details-row"><span class="meta">Snapshot</span>snapshot_status {_budget_value(budget.get("snapshot_status"))} · observed_at {_budget_value(budget.get("observed_at"))} · enforcement_state_exact {_budget_value(budget.get("enforcement_state_exact"))}</div>'
        f'{"".join(_budget_run_details_html(run) for run in (runs or [])) or "<span class=meta>Запусков в budget нет</span>"}'
        '</div>'
    )


def _session_payload(session, store, ledger=None, budget_context=None) -> dict:
    tickets = [store.get(ticket_id) for ticket_id in session.participants if _ticket_exists(store, ticket_id)]
    aggregate = {
        "mandatory": sum(ticket.mandatory for ticket in tickets),
        "optional": sum(not ticket.mandatory for ticket in tickets),
        "done": sum(store.is_done(ticket) for ticket in tickets),
        "blocked": sum(bool(ticket.blocked_by) for ticket in tickets),
        "active_run": sum(bool(ticket.active_run) for ticket in tickets),
    }
    budget, budget_runs = _budget_read_model(ledger, f"session:{session.id}", budget_context) if ledger else (None, [])
    aggregate["budget_started_runs"] = budget["started_runs"] if budget else 0
    aggregate["budget_reserved_runs"] = budget["reserved_runs"] if budget else 0
    return {"session": session.to_dict(), "aggregate": aggregate, "budget": budget,
            "budget_runs": budget_runs, "tickets": [_ticket_payload(ticket, ledger, budget_context) for ticket in tickets]}


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


def _ticket_payload(ticket, ledger=None, budget_context=None) -> dict:
    payload = ticket.to_dict()
    latest, aggregate = _ticket_usage(ticket)
    if aggregate["confirmed_runs"] or ticket.run_history:
        payload["token_usage"] = latest
        payload["token_usage_aggregate"] = aggregate
    budget, budget_runs = _budget_read_model(ledger, f"ticket:{ticket.id}", budget_context) if ledger else (None, [])
    payload["budget"] = budget
    payload["budget_runs"] = budget_runs
    by_id = {run["run_id"]: run for run in budget_runs}
    payload["run_history"] = [dict(entry, budget_run=by_id[entry["run_id"]]) if entry.get("run_id") in by_id else entry
                               for entry in payload.get("run_history", [])]
    return payload


def _sessions_html(store, session_store, ledger=None, budget_context=None) -> str:
    if not session_store:
        session_store = DeliverySessionStore(store.project)
    sessions = session_store.list()
    cards = []
    for session in sessions:
        payload = _session_payload(session, store, ledger, budget_context)
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
            available_tickets = [
                ticket for ticket in store.list("delivery")
                if not store.is_done(ticket) and ticket.id not in session.participants
            ]
            options = "".join(
                f'<option value="{html.escape(ticket.id)}">{html.escape(ticket.id)} · {html.escape(ticket.title)}</option>'
                for ticket in available_tickets
            )
            if not options:
                options = '<option value="" selected disabled>Нет доступных тикетов</option>'
            add_disabled = " disabled" if not available_tickets else ""
            actions = f'<form method="post" action="/session/add"><input type="hidden" name="session" value="{html.escape(session.id)}"><select name="ticket" required>{options}</select><button{add_disabled}>Добавить тикет</button></form><form method="post" action="/session/activate"><input type="hidden" name="session" value="{html.escape(session.id)}"><button>Активировать</button></form>'
        elif session.status == "active":
            actions = f'<form method="post" action="/session/complete"><input type="hidden" name="session" value="{html.escape(session.id)}"><input name="override" placeholder="Причина override, если неполна"><button>Завершить</button></form><form method="post" action="/session/cancel"><input type="hidden" name="session" value="{html.escape(session.id)}"><input name="override" placeholder="Причина override, если неполна"><button>Отменить</button></form>'
        cards.append(f'<article class="card"><strong>{html.escape(session.title)}</strong><span class="badge">{html.escape(session.id)}</span><span class="badge">{html.escape(session.status)}</span><div class="meta">mandatory {aggregate["mandatory"]} · optional {aggregate["optional"]} · done {aggregate["done"]} · blocked {aggregate["blocked"]} · active_run {aggregate["active_run"]}</div>{_budget_summary_html(payload["budget"])}<div class="details-body">{"".join(participants) or "<span class=meta>Состав пуст</span>"}</div>{actions}</article>')
    create = '<form class="create-form" method="post" action="/session/create"><strong>Новая Delivery-сессия</strong><input name="title" placeholder="название сессии" required><button>Создать</button></form>'
    return f'<section class="sessions"><h2>Delivery-сессии</h2>{create}{"".join(cards) or "<div class=meta>Сессий пока нет</div>"}</section>'


def _ticket_details_html(ticket, tree=None, ledger=None, budget_context=None) -> str:
    parent = ticket.parent or "нет"
    blockers = ", ".join(ticket.blocked_by) if ticket.blocked_by else "нет"
    outcome = ticket.last_outcome or "нет"
    retry_after = ticket.retry_after or "нет"
    description = ticket.description or "(пусто)"
    latest_usage, aggregate = _ticket_usage(ticket)
    budget, budget_runs = _budget_read_model(ledger, f"ticket:{ticket.id}", budget_context) if ledger else (None, [])
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
        f'{_budget_summary_html(budget)}{_budget_details_html(budget, budget_runs)}'
        f'{tree_details}'
        f'<div class="details-row"><span class="meta">Создан</span>{html.escape(ticket.created_at)}</div>'
        f'<div class="details-row"><span class="meta">Обновлен</span>{html.escape(ticket.updated_at)}</div>'
        "</div></details>"
    )


def _ticket_action_html(store, workflow, ticket) -> str:
    target = next_status_for_ticket(store, ticket)
    if target:
        return f'<form method="post" action="/move"><input type="hidden" name="id" value="{html.escape(ticket.id)}"><input type="hidden" name="target" value="{html.escape(target)}"><button>Переместить → {html.escape(workflow.by_id[target].title)}</button></form>'
    if workflow.by_id.get(ticket.status) and workflow.by_id[ticket.status].kind == "agent" and retry_exhausted(ticket) and not ticket.blocked_by:
        return f'<form method="post" action="/retry"><input type="hidden" name="id" value="{html.escape(ticket.id)}"><button>Повторить</button></form>'
    if ticket.status == "ready_for_release" and ticket.last_outcome == "integration_conflict":
        return f'<form method="post" action="/release-retry"><input type="hidden" name="id" value="{html.escape(ticket.id)}"><button>Повторить интеграцию</button></form>'
    return ""


def _ticket_drawer_html(store, workflows, process, tickets, tree_manager, session_store, ledger=None, budget_context=None) -> str:
    panels = [_ticket_drawer_panel_html(store, workflows, process, ticket, tree_manager, session_store, ledger, budget_context) for ticket in tickets]
    return f'<div class="drawer-backdrop" data-drawer-backdrop hidden></div><aside class="ticket-drawer" data-ticket-drawer role="dialog" aria-modal="true" aria-label="Контекст тикета" aria-hidden="true" hidden>{"".join(panels)}</aside>'


def _ticket_drawer_panel_html(store, workflows, process, ticket, tree_manager, session_store, ledger=None, budget_context=None) -> str:
    workflow = workflows.get(ticket.process) or workflows[process]
    tree = tree_manager.trees.get(ticket.id) if tree_manager else None
    session = next((item for item in session_store.list() if ticket.id in item.participants), None) if session_store and ticket.process == "delivery" else None
    parent = ticket.parent or "нет"
    blockers = ", ".join(ticket.blocked_by) if ticket.blocked_by else "нет"
    run_links = []
    for entry in reversed(ticket.run_history):
        run_id = entry.get("run_id")
        if not run_id:
            continue
        artifact_path = entry.get("artifacts_path") or f".vibe/runs/{run_id}"
        run_links.append(
            f'<div class="run-entry{" active-run" if run_id == ticket.active_run else ""}"><span class="badge">{html.escape(str(entry.get("event", "run")))}</span> '
            f'<span class="meta">{html.escape(str(entry.get("stage", "")))} · {html.escape(str(entry.get("timestamp", "")))}</span>'
            f'<div>{html.escape(str(entry.get("summary", "")))}</div><a href="/artifacts/{urllib.parse.quote(str(run_id), safe="")}" target="_blank" rel="noopener">Артефакты: {html.escape(str(artifact_path))}</a></div>'
        )
    tree_html = ""
    if tree:
        tree_html = f'<div class="details-row"><span class="meta">Ветка</span>{html.escape(tree.branch)}</div><div class="details-row"><span class="meta">Worktree</span>{html.escape(tree.worktree)}</div><div class="details-row"><span class="meta">Интеграция</span>{html.escape(tree.integration_status)}</div>'
    session_html = f'<div class="details-row"><span class="meta">Сессия</span>{html.escape(session.id)} · {html.escape(session.status)}</div>' if session else '<div class="details-row"><span class="meta">Сессия</span>нет</div>'
    action = _ticket_action_html(store, workflow, ticket)
    budget, budget_runs = _budget_read_model(ledger, f"ticket:{ticket.id}", budget_context) if ledger else (None, [])
    budget_details = _budget_summary_html(budget)
    run_details = _budget_details_html(budget, budget_runs)
    return (
        f'<section class="drawer-panel" data-drawer-ticket="{html.escape(ticket.id)}" tabindex="-1" hidden>'
        f'<div class="drawer-header"><div><span class="meta">{html.escape(ticket.id)}</span><h2>{html.escape(ticket.title)}</h2></div><button type="button" class="drawer-close" data-drawer-close aria-label="Закрыть drawer">Закрыть</button></div>'
        f'<div class="drawer-actions">{action}</div><div class="drawer-section"><h3>Контекст тикета</h3>'
        f'<div class="details-row"><span class="meta">Тип · статус · приоритет</span>{html.escape(ticket.type)} · {html.escape(ticket.status)} · {ticket.priority}</div>'
        f'<div class="details-row"><span class="meta">Описание</span><div class="drawer-description">{html.escape(ticket.description or "(пусто)")}</div></div>'
        f'<div class="details-row"><span class="meta">Summary</span>{html.escape(ticket.last_summary or "нет")}</div>'
        f'<div class="details-row"><span class="meta">Outcome</span>{html.escape(ticket.last_outcome or "нет")}</div>'
        f'<div class="details-row"><span class="meta">Родитель · blockers</span>{html.escape(parent)} · {html.escape(blockers)}</div>'
        f'<div class="details-row"><span class="meta">Создан · обновлен</span>{html.escape(ticket.created_at)} · {html.escape(ticket.updated_at)}</div></div>'
        f'<div class="drawer-section"><h3>Budget</h3>{budget_details}{run_details or "<span class=meta>нет enforced budget</span>"}</div>'
        f'<div class="drawer-section"><h3>Retry и выполнение</h3><div class="details-row"><span class="meta">Active run</span>{html.escape(ticket.active_run or "нет")}</div><div class="details-row"><span class="meta">Ошибок подряд · повтор после</span>{ticket.consecutive_failures} · {html.escape(ticket.retry_after or "нет")}</div>{session_html}{tree_html}</div>'
        f'<div class="drawer-section"><h3>История запусков</h3><div class="run-history">{"".join(run_links) or "<span class=meta>Запусков пока нет</span>"}</div></div></section>'
    )
