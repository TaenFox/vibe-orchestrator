from pathlib import Path
from threading import Thread

import pytest

from vibe_orchestrator.tickets import TicketStore
from vibe_orchestrator.ui import _build_server
from tests.ui_server_fixture import UiServerFixture


@pytest.fixture
def project(tmp_path: Path) -> Path:
    store = TicketStore(tmp_path)
    store.init()
    return tmp_path


@pytest.fixture
def http_server(project):
    """Run the UI HTTP server against the isolated test project."""
    try:
        server = _build_server(project, "127.0.0.1", 0)
    except PermissionError:
        pytest.skip("The test environment does not permit binding a local HTTP port")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def ui_server(project):
    """Factory for the serial subprocess UI capability adapter."""
    def factory(command, **kwargs):
        return UiServerFixture(command, project_root=project, **kwargs)
    return factory
