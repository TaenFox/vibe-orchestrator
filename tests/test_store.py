from pathlib import Path
from vibe_orchestrator.tickets import TicketStore


def test_create_and_reload(tmp_path: Path):
    store=TicketStore(tmp_path); store.init(); created=store.create("discovery","idea","Test idea",description="Hello"); loaded=store.get(created.id); assert loaded.title=="Test idea"; assert loaded.status=="todo"; assert loaded.description=="Hello"


def test_rework_defaults_to_wip_exempt(tmp_path: Path):
    store=TicketStore(tmp_path); store.init(); ticket=store.create("delivery","rework","Fix review",parent="DEL-ABC"); assert ticket.wip_exempt is True
