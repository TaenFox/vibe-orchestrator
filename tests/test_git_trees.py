import subprocess
from pathlib import Path

from vibe_orchestrator.git_trees import GitTreeManager
from vibe_orchestrator.orchestrator import Orchestrator
from vibe_orchestrator.tickets import TicketStore


def git(project: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=project, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def git_project(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.com")
    git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / ".gitignore").write_text(".vibe/\n", encoding="utf-8")
    (tmp_path / "app.txt").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "app.txt", ".gitignore")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def test_delivery_tree_is_merged_into_main_on_release(tmp_path: Path):
    project = git_project(tmp_path)
    store = TicketStore(project)
    store.init()
    manager = GitTreeManager(project, store)
    ticket = store.create("delivery", "task", "Tree release")

    workspace = manager.workspace_for(ticket)
    assert workspace is not None
    assert workspace.exists()
    assert git(workspace, "branch", "--show-current") == f"vibe/{ticket.id.lower()}"

    (workspace / "app.txt").write_text("implemented\n", encoding="utf-8")
    manager.release(ticket)

    assert (project / "app.txt").read_text(encoding="utf-8") == "implemented\n"
    assert not workspace.exists()
    assert manager.trees.get(ticket.id).integration_status == "merged"


def test_rework_tree_starts_from_parent_tree_and_targets_parent_branch(tmp_path: Path):
    project = git_project(tmp_path)
    store = TicketStore(project)
    store.init()
    manager = GitTreeManager(project, store)
    parent = store.create("delivery", "task", "Parent tree")
    parent_workspace = manager.workspace_for(parent)
    (parent_workspace / "app.txt").write_text("parent\n", encoding="utf-8")
    child = store.create("delivery", "rework", "Fix parent", parent=parent.id)

    child_workspace = manager.workspace_for(child)

    assert (child_workspace / "app.txt").read_text(encoding="utf-8") == "parent\n"
    assert git(child_workspace, "merge-base", "HEAD", f"vibe/{parent.id.lower()}") == git(child_workspace, "rev-parse", "HEAD")


def test_orchestrator_releases_ready_ticket_into_main(tmp_path: Path):
    project = git_project(tmp_path)
    orchestrator = Orchestrator(project, max_agents=0)
    ticket = orchestrator.store.create("delivery", "task", "Automatic release", status="ready_for_release")
    workspace = orchestrator.tree_manager.workspace_for(ticket)
    (workspace / "app.txt").write_text("released\n", encoding="utf-8")

    orchestrator._reconcile_tickets()

    assert orchestrator.store.get(ticket.id).status == "done"
    assert (project / "app.txt").read_text(encoding="utf-8") == "released\n"
