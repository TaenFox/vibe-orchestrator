from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from .tickets import Ticket, TicketStore


class GitTreeError(RuntimeError):
    pass


@dataclass
class TreeRecord:
    ticket_id: str
    branch: str
    base_branch: str
    worktree: str
    integration_status: str = "active"
    integration_error: str | None = None


class TreeStore:
    def __init__(self, project: Path):
        self.path = project.resolve() / ".vibe" / "tmp" / "trees.yaml"

    def get(self, ticket_id: str) -> TreeRecord | None:
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        item = payload.get(ticket_id) if isinstance(payload, dict) else None
        return TreeRecord(**item) if isinstance(item, dict) else None

    def save(self, record: TreeRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            payload = {}
        payload[record.ticket_id] = asdict(record)
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class GitTreeManager:
    def __init__(self, project: Path, store: TicketStore):
        self.project = project.resolve()
        self.store = store
        self.trees = TreeStore(self.project)
        self.worktrees_root = self.project / ".vibe" / "tmp" / "worktrees"
        self.main_branch = os.environ.get("VIBE_MAIN_BRANCH", "main")
        configured_main_worktree = os.environ.get("VIBE_MAIN_WORKTREE")
        self.main_worktree = Path(configured_main_worktree).resolve() if configured_main_worktree else None

    def enabled(self) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.project), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def workspace_for(self, ticket: Ticket, *, stage_id: str | None = None) -> Path | None:
        if ticket.process != "delivery" or not self.enabled():
            return None
        handle = self.ensure_tree(ticket)
        if stage_id == "development":
            self.sync_with_main(handle)
        return handle.worktree_path

    def sync_with_main(self, handle: "TreeHandle") -> None:
        """Bring the current main branch into a ticket tree before coding starts."""
        self.commit_workspace(handle.worktree_path, handle.record.ticket_id)
        self._ensure_main_worktree()
        try:
            self._run(["merge", "--no-edit", self.main_branch], cwd=handle.worktree_path)
        except GitTreeError:
            self._run_optional(["merge", "--abort"], cwd=handle.worktree_path)
            raise

    def ensure_tree(self, ticket: Ticket) -> "TreeHandle":
        if ticket.process != "delivery":
            raise GitTreeError("Git-деревья поддерживаются только для Delivery-тикетов")
        record = self.trees.get(ticket.id)
        if record and Path(record.worktree).exists():
            return TreeHandle(record, self)

        base_branch = self.main_branch
        if ticket.parent:
            parent = self.store.get(ticket.parent)
            if parent.process == "delivery":
                parent_handle = self.ensure_tree(parent)
                self.commit_workspace(parent_handle.worktree_path, parent.id)
                base_branch = parent_handle.record.branch

        branch = record.branch if record else f"vibe/{ticket.id.lower()}"
        worktree = self.worktrees_root / ticket.id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if record and not worktree.exists():
            self._run(["worktree", "add", str(worktree), branch], cwd=self.project)
        elif not record:
            branch_exists = self._run_optional(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=self.project)
            if branch_exists:
                self._run(["worktree", "add", str(worktree), branch], cwd=self.project)
            else:
                self._run(["worktree", "add", "-b", branch, str(worktree), base_branch], cwd=self.project)
            record = TreeRecord(ticket.id, branch, base_branch, str(worktree))
            self.trees.save(record)
        else:
            raise GitTreeError(f"Worktree для {ticket.id} не найден: {worktree}")
        return TreeHandle(record, self)

    def commit_workspace(self, worktree: Path, ticket_id: str) -> None:
        status = self._run(["status", "--porcelain"], cwd=worktree).stdout.strip()
        if not status:
            return
        self._run(["add", "-A"], cwd=worktree)
        self._run(["commit", "-m", f"Обновить дерево тикета {ticket_id}"], cwd=worktree)

    def release(self, ticket: Ticket) -> TreeRecord:
        handle = self.ensure_tree(ticket)
        record = handle.record
        target_worktree: Path | None = None
        try:
            self.commit_workspace(handle.worktree_path, ticket.id)
            target_branch, target_worktree = self._integration_target(ticket)
            current_branch = self._run(["branch", "--show-current"], cwd=target_worktree).stdout.strip()
            if current_branch != target_branch:
                raise GitTreeError(f"Ожидалась ветка {target_branch} в {target_worktree}, получена {current_branch or '(detached)'}")
            self._run(["merge", "--no-ff", record.branch, "-m", f"Интегрировать дерево тикета {ticket.id}"], cwd=target_worktree)
        except GitTreeError as exc:
            record.integration_status = "conflict"
            record.integration_error = str(exc)
            self.trees.save(record)
            if target_worktree is not None:
                self._run_optional(["merge", "--abort"], cwd=target_worktree)
            raise
        record.integration_status = "merged"
        record.integration_error = None
        self.trees.save(record)
        if handle.worktree_path.exists():
            self._run(["worktree", "remove", "--force", str(handle.worktree_path)], cwd=self.project)
        return record

    def reset_integration(self, ticket_id: str) -> bool:
        record = self.trees.get(ticket_id)
        if not record or record.integration_status != "conflict":
            return False
        record.integration_status = "active"
        record.integration_error = None
        self.trees.save(record)
        return True

    def _integration_target(self, ticket: Ticket) -> tuple[str, Path]:
        if ticket.parent:
            parent = self.store.get(ticket.parent)
            if parent.process == "delivery":
                parent_record = self.trees.get(parent.id)
                if parent_record:
                    return parent_record.branch, Path(parent_record.worktree)
        return self.main_branch, self._ensure_main_worktree()

    def _ensure_main_worktree(self) -> Path:
        if self.main_worktree is not None:
            return self.main_worktree
        for path, branch in self._registered_worktrees():
            if branch == self.main_branch:
                self.main_worktree = path
                return path
        current_branch = self._run(["branch", "--show-current"], cwd=self.project).stdout.strip()
        if current_branch == self.main_branch:
            self.main_worktree = self.project
            return self.project
        if not self._run_optional(["show-ref", "--verify", f"refs/heads/{self.main_branch}"], cwd=self.project):
            raise GitTreeError(f"Не найдена целевая ветка {self.main_branch!r} для интеграции")
        worktree = self.worktrees_root / "__main__"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._run(["worktree", "add", str(worktree), self.main_branch], cwd=self.project)
        self.main_worktree = worktree.resolve()
        return self.main_worktree

    def _registered_worktrees(self) -> list[tuple[Path, str]]:
        output = self._run(["worktree", "list", "--porcelain"], cwd=self.project).stdout
        records: list[tuple[Path, str]] = []
        path: Path | None = None
        for line in output.splitlines() + [""]:
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree ")).resolve()
            elif line.startswith("branch refs/heads/") and path is not None:
                records.append((path, line.removeprefix("branch refs/heads/")))
                path = None
            elif not line and path is not None:
                path = None
        return records

    def _run(self, args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise GitTreeError(f"git {' '.join(args)}: {detail}")
        return result

    def _run_optional(self, args: list[str], *, cwd: Path) -> bool:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        return result.returncode == 0


@dataclass
class TreeHandle:
    record: TreeRecord
    manager: GitTreeManager

    @property
    def worktree_path(self) -> Path:
        return Path(self.record.worktree)
