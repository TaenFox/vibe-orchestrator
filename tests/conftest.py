from pathlib import Path

import pytest

from vibe_orchestrator.tickets import TicketStore


@pytest.fixture
def project(tmp_path: Path) -> Path:
    store = TicketStore(tmp_path)
    store.init()
    return tmp_path
