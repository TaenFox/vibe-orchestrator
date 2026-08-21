from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vibe_orchestrator.tickets import TicketStore
from tests.ui_server_fixture import CAPABILITY_FAILURE, UiServerError


UI_RUNNER = """
import signal, sys
from pathlib import Path
from vibe_orchestrator.ui import _build_server

project, host, port = sys.argv[1:]
server = _build_server(Path(project), host, int(port))
signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
server.serve_forever()
"""


@pytest.fixture
def browser_ticket(project):
    return TicketStore(project).create(
        "discovery",
        "idea",
        "Browser smoke card",
        description="Description\nwith a second line",
        status="todo",
        priority=7,
    )


@pytest.fixture
def browser_page(project, browser_ticket, ui_server):
    """Start the production UI and an isolated persistent Chromium context."""
    fixture = ui_server(
        [sys.executable, "-c", UI_RUNNER, "{project_root}", "{host}", "{port}"],
        test_id="browser-ui",
        retention="failure",
    )
    started = False
    try:
        try:
            fixture.start()
            started = True
        except UiServerError as exc:
            if exc.classification == CAPABILITY_FAILURE:
                pytest.skip(f"UI server capability unavailable: {exc}")
            raise
        try:
            page = fixture.start_browser(browser_name="chromium")
        except UiServerError as exc:
            if exc.classification == CAPABILITY_FAILURE:
                pytest.skip(f"Chromium capability unavailable: {exc}")
            raise
        yield page
    finally:
        if started:
            fixture.teardown()
