import subprocess
from pathlib import Path

from vibe_orchestrator.codex import AgentResult
from vibe_orchestrator.git_trees import GitTreeError, GitTreeManager
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


def test_release_branch_mismatch_is_recorded_as_conflict(tmp_path: Path, monkeypatch):
    project = git_project(tmp_path)
    store = TicketStore(project)
    store.init()
    manager = GitTreeManager(project, store)
    ticket = store.create("delivery", "task", "Wrong target branch", status="ready_for_release")
    manager.workspace_for(ticket)
    monkeypatch.setenv("VIBE_MAIN_BRANCH", "stable")
    manager = GitTreeManager(project, store)

    try:
        manager.release(ticket)
    except Exception as exc:
        assert "Не найдена целевая ветка 'stable'" in str(exc)
    else:
        raise AssertionError("release должен завершиться конфликтом ветки")

    assert manager.trees.get(ticket.id).integration_status == "conflict"


def test_release_from_stable_automatically_creates_main_worktree(tmp_path: Path):
    project = git_project(tmp_path)
    git(project, "checkout", "-b", "stable")
    store = TicketStore(project)
    store.init()
    manager = GitTreeManager(project, store)
    ticket = store.create("delivery", "task", "Automatic main target", status="ready_for_release")
    workspace = manager.workspace_for(ticket)
    (workspace / "app.txt").write_text("released automatically\n", encoding="utf-8")

    manager.release(ticket)

    assert manager.main_worktree is not None
    assert manager.main_worktree != project
    assert (manager.main_worktree / "app.txt").read_text(encoding="utf-8") == "released automatically\n"


def test_development_tree_is_synced_with_current_main(tmp_path: Path):
    project = git_project(tmp_path)
    git(project, "checkout", "-b", "stable")
    store = TicketStore(project)
    store.init()
    manager = GitTreeManager(project, store)
    ticket = store.create("delivery", "task", "Sync before development")

    main_worktree = manager._ensure_main_worktree()
    (main_worktree / "main-change.txt").write_text("current main\n", encoding="utf-8")
    git(main_worktree, "add", "main-change.txt")
    git(main_worktree, "commit", "-m", "main change")

    workspace = manager.workspace_for(ticket, stage_id="development")

    assert (workspace / "main-change.txt").read_text(encoding="utf-8") == "current main\n"


def test_development_sync_combines_independently_added_text_files(tmp_path: Path):
    project = git_project(tmp_path)
    git(project, "checkout", "-b", "stable")
    store = TicketStore(project)
    store.init()
    manager = GitTreeManager(project, store)
    ticket = store.create("delivery", "task", "Resolve add-add")
    workspace = manager.workspace_for(ticket)
    (workspace / "shared.txt").write_text("ticket change\n", encoding="utf-8")
    manager.commit_workspace(workspace, ticket.id)

    main_worktree = manager._ensure_main_worktree()
    (main_worktree / "shared.txt").write_text("main change\n", encoding="utf-8")
    git(main_worktree, "add", "shared.txt")
    git(main_worktree, "commit", "-m", "main shared change")

    manager.workspace_for(ticket, stage_id="development")

    content = (workspace / "shared.txt").read_text(encoding="utf-8")
    assert "ticket change" in content
    assert "main change" in content
    assert not git(workspace, "status", "--porcelain")


def test_development_sync_leaves_content_conflict_for_agent(tmp_path: Path):
    project = git_project(tmp_path)
    (project / "shared.txt").write_text("base version\n", encoding="utf-8")
    git(project, "add", "shared.txt")
    git(project, "commit", "-m", "shared base")
    git(project, "checkout", "-b", "stable")
    store = TicketStore(project)
    store.init()
    manager = GitTreeManager(project, store)
    ticket = store.create("delivery", "task", "Resolve content conflict")
    workspace = manager.workspace_for(ticket)
    (workspace / "shared.txt").write_text("ticket version\n", encoding="utf-8")
    manager.commit_workspace(workspace, ticket.id)

    main_worktree = manager._ensure_main_worktree()
    (main_worktree / "shared.txt").write_text("main version\n", encoding="utf-8")
    git(main_worktree, "add", "shared.txt")
    git(main_worktree, "commit", "-m", "main conflicting change")

    assert manager.workspace_for(ticket, stage_id="development") == workspace
    assert git(workspace, "diff", "--name-only", "--diff-filter=U") == "shared.txt"
    try:
        manager.commit_workspace(workspace, ticket.id)
    except GitTreeError as exc:
        assert "неразрешенные Git-конфликты" in str(exc)
    else:
        raise AssertionError("Неразрешенный конфликт не должен попасть в коммит")


def test_completed_rework_is_integrated_before_parent_is_unblocked(tmp_path: Path):
    project = git_project(tmp_path)
    orchestrator = Orchestrator(project, max_agents=0)
    parent = orchestrator.store.create("delivery", "task", "Parent", status="review")
    parent_workspace = orchestrator.tree_manager.workspace_for(parent)
    child = orchestrator.store.create(
        "delivery", "rework", "Rework", parent=parent.id, status="review", rework_stage="review"
    )
    child_workspace = orchestrator.tree_manager.workspace_for(child)
    (child_workspace / "app.txt").write_text("reworked\n", encoding="utf-8")
    parent.blocked_by = [child.id]
    parent.last_outcome = "needs_rework"
    orchestrator.store.save(parent)

    workflow = orchestrator.workflows["delivery"]
    orchestrator._apply_result(
        workflow,
        child.id,
        workflow.by_id["review"],
        AgentResult(outcome="completed", summary="Исправлено", details=""),
    )

    assert orchestrator.store.get(child.id).status == "done"
    assert orchestrator.store.get(parent.id).blocked_by == []
    assert (parent_workspace / "app.txt").read_text(encoding="utf-8") == "reworked\n"
    assert not child_workspace.exists()
