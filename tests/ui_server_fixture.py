"""Reusable subprocess fixture for serial local UI checks.

This module deliberately owns only the process group it starts.  It is a test
capability adapter, not a browser runner, and is supported on POSIX systems.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


CAPABILITY_FAILURE = "capability_environment_failure"
PRODUCT_FAILURE = "ui_product_failure"
SCHEMA_VERSION = 1
ARTIFACT_RETENTION_ENV = "BROWSER_ARTIFACT_RETENTION"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UiServerDiagnostics:
    artifact_dir: Path
    metadata_path: Path
    stdout_path: Path
    stderr_path: Path
    data_root: Path
    command: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    requested_port: int = 0
    assigned_port: int | None = None
    base_url: str | None = None
    readiness_url: str | None = None
    expected_readiness_status: int = 200
    pid: int | None = None
    pgid: int | None = None
    classification: str | None = None
    readiness: dict[str, object] = field(default_factory=dict)
    returncode: int | None = None
    termination_signals: list[str] = field(default_factory=list)
    process_alive_before: bool | None = None
    process_alive_after: bool | None = None
    process_group_alive_after: bool | None = None
    environment_overrides: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    readiness_deadline: str | None = None
    stopped_at: str | None = None
    test_id: str = "ui-server"
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state_root: Path | None = None
    manifest_path: Path | None = None
    outcome: str | None = None
    browser_name: str | None = None
    browser_version: str | None = None
    browser_reason: str | None = None
    browser_cache_path: Path | None = None
    browser_profile_path: Path | None = None
    browser_state_path: Path | None = None
    port_attempts: int = 0
    port_retries: int = 0
    port_errors: list[str] = field(default_factory=list)
    worktree: Path | None = None
    retention_policy: str = "delete-transient-on-success"
    retention_status: str = "pending"
    files: dict[str, dict[str, object]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "command": self.command,
            "environment_overrides": self.environment_overrides,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "readiness_deadline": self.readiness_deadline,
            "stopped_at": self.stopped_at,
            "host": self.host,
            "requested_port": self.requested_port,
            "assigned_port": self.assigned_port,
            "base_url": self.base_url,
            "readiness_url": self.readiness_url,
            "expected_readiness_status": self.expected_readiness_status,
            "data_root": str(self.data_root),
            "artifact_dir": str(self.artifact_dir),
            "pid": self.pid,
            "pgid": self.pgid,
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "classification": self.classification,
            "readiness": self.readiness,
            "returncode": self.returncode,
            "termination_signals": self.termination_signals,
            "process_alive_before": self.process_alive_before,
            "process_alive_after": self.process_alive_after,
            "process_group_alive_after": self.process_group_alive_after,
            "test_id": self.test_id,
            "run_id": self.run_id,
            "outcome": self.outcome,
            "browser_name": self.browser_name,
            "browser_version": self.browser_version,
            "browser_reason": self.browser_reason,
            "browser_paths": {
                "cache": str(self.browser_cache_path) if self.browser_cache_path else None,
                "profile": str(self.browser_profile_path) if self.browser_profile_path else None,
                "state": str(self.browser_state_path) if self.browser_state_path else None,
            },
            "port_attempts": self.port_attempts,
            "port_retries": self.port_retries,
            "port_errors": self.port_errors,
            "url": self.base_url,
            "port": self.assigned_port,
            "worktree": str(self.worktree or self.data_root),
            "artifact_root": str(self.artifact_dir),
            "state_root": str(self.state_root) if self.state_root else None,
            "timestamps": {"created": self.created_at, "started": self.started_at, "stopped": self.stopped_at},
            "owned_process": {"pid": self.pid, "pgid": self.pgid, "signals": self.termination_signals},
            "files": self.files,
            "retention": {"policy": self.retention_policy, "status": self.retention_status},
        }

    def save(self) -> None:
        payload = json.dumps(self.as_dict(), indent=2, ensure_ascii=False)
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_path.with_name(f".{self.metadata_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.metadata_path)
        if self.manifest_path and self.manifest_path != self.metadata_path:
            temporary_manifest = self.manifest_path.with_name(f".{self.manifest_path.name}.{uuid.uuid4().hex}.tmp")
            temporary_manifest.write_text(payload, encoding="utf-8")
            temporary_manifest.replace(self.manifest_path)


class UiServerError(RuntimeError):
    """A capability failure with stable classification and persisted diagnostics."""

    def __init__(self, message: str, *, classification: str, diagnostics: UiServerDiagnostics, cause: BaseException | None = None):
        super().__init__(message)
        self.classification = classification
        self.diagnostics = diagnostics
        self.cause = cause


CommandFactory = Callable[[Path, str, int], Sequence[str]]


class UiServerFixture:
    """Start a documented UI command in an isolated, owned process group.

    Instances are intentionally serial.  The temporary artifact directory is
    retained after exit so failure diagnostics can be inspected by the caller.
    """

    def __init__(
        self,
        command: Sequence[str] | CommandFactory,
        *,
        project_root: Path | None = None,
        test_id: str | None = None,
        artifact_base: Path | None = None,
        retention: str | None = None,
        host: str = "127.0.0.1",
        readiness_path: str = "/",
        expected_readiness_status: int = 200,
        readiness_timeout: float = 10.0,
        readiness_interval: float = 0.05,
        graceful_timeout: float = 2.0,
        port_attempts: int = 3,
        env: dict[str, str] | None = None,
    ) -> None:
        if os.name != "posix":
            raise UiServerError("UI subprocess fixture requires POSIX process groups", classification=CAPABILITY_FAILURE,
                                diagnostics=self._placeholder_diagnostics())
        self.command_template = command
        self.source_root = Path(project_root).resolve() if project_root else None
        self.test_id = (test_id or os.environ.get("PYTEST_CURRENT_TEST", "ui-server")).split(" ")[0]
        self.test_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in self.test_id)[:120] or "ui-server"
        self.artifact_base = Path(artifact_base).resolve() if artifact_base else (
            self.source_root / ".vibe" / "browser-artifacts" if self.source_root else None
        )
        self.retention = retention or os.environ.get(ARTIFACT_RETENTION_ENV, "failure")
        if self.retention not in {"failure", "always"}:
            raise ValueError("retention must be 'failure' or 'always'")
        self._host = host
        self.readiness_path = readiness_path if readiness_path.startswith("/") else f"/{readiness_path}"
        if not isinstance(expected_readiness_status, int):
            raise TypeError("expected_readiness_status must be an integer")
        self.expected_readiness_status = expected_readiness_status
        self.readiness_timeout = readiness_timeout
        self.readiness_interval = readiness_interval
        self.graceful_timeout = graceful_timeout
        self.port_attempts = port_attempts
        if self.port_attempts < 1:
            raise ValueError("port_attempts must be at least 1")
        self.env_overrides = dict(env or {})
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout = None
        self._stderr = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.diagnostics = self._placeholder_diagnostics()
        self.diagnostics.worktree = self.source_root or self.diagnostics.data_root
        self.diagnostics.host = host
        self.diagnostics.expected_readiness_status = expected_readiness_status
        self.diagnostics.environment_overrides = {
            key: "<redacted>" if any(token in key.upper() for token in ("SECRET", "TOKEN", "PASSWORD", "KEY")) else value
            for key, value in self.env_overrides.items()
        }

    def _placeholder_diagnostics(self) -> UiServerDiagnostics:
        if self.artifact_base:
            artifact_base = self.artifact_base / self.test_id
            artifact_base.mkdir(parents=True, exist_ok=True)
            artifact_dir = artifact_base / uuid.uuid4().hex
            artifact_dir.mkdir()
        else:
            artifact_dir = Path(tempfile.mkdtemp(prefix="vibe-ui-fixture-"))
        data_root = artifact_dir / "project"
        data_root.mkdir()
        state_root = artifact_dir / "state"
        state_root.mkdir()
        return UiServerDiagnostics(
            artifact_dir, artifact_dir / "metadata.json", artifact_dir / "server.stdout.log",
            artifact_dir / "server.stderr.log", data_root, test_id=self.test_id,
            state_root=state_root, manifest_path=artifact_dir / "manifest.json",
            retention_policy="retain-on-failure/delete-transient-on-success" if self.retention == "failure" else "always",
        )

    def _prepare(self) -> None:
        if self.source_root:
            # The artifact base lives under the project .vibe directory.  Do
            # not copy it into the per-run project or a run would recursively
            # copy its own evidence and leak other runs' state.
            shutil.copytree(
                self.source_root, self.diagnostics.data_root, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "browser-artifacts"),
            )

    def _port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self._host, 0))
            return int(sock.getsockname()[1])

    def _argv(self, port: int) -> list[str]:
        if callable(self.command_template):
            return [str(value) for value in self.command_template(self.diagnostics.data_root, self._host, port)]
        values = {"project_root": str(self.diagnostics.data_root), "host": self._host, "port": str(port)}
        return [str(value).format(**values) for value in self.command_template]

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def base_url(self) -> str:
        if self.diagnostics.base_url is None:
            raise RuntimeError("UI server has not started")
        return self.diagnostics.base_url

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self.diagnostics.assigned_port is None:
            raise RuntimeError("UI server has not started")
        return self.diagnostics.assigned_port

    @property
    def data_root(self) -> Path:
        return self.diagnostics.data_root

    def _group_exists(self) -> bool:
        if self.diagnostics.pgid is None:
            return False
        try:
            os.killpg(self.diagnostics.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _write_state(self) -> None:
        self.diagnostics.process_alive_after = self._alive()
        self.diagnostics.process_group_alive_after = self._group_exists()
        if self.process is not None:
            self.diagnostics.returncode = self.process.poll()
        self.diagnostics.save()

    def _file_status(self, name: str, path: Path, *, reason: str | None = None) -> None:
        descriptor: dict[str, object] = {"path": str(path), "available": path.exists()}
        if path.exists():
            descriptor["size_bytes"] = path.stat().st_size
        if reason:
            descriptor["reason"] = reason
        self.diagnostics.files[name] = descriptor

    def start_browser(self, url: str | None = None, *, browser_name: str = "chromium"):
        """Create a fresh Playwright context for this run and start tracing.

        Playwright is deliberately imported lazily: serial HTTP checks do not
        require the optional browser dependency or a locally installed binary.
        """
        target = url or self.base_url
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            browser_type = getattr(self._playwright, browser_name)
            state_root = self.diagnostics.state_root
            assert state_root is not None
            cache_path = state_root / "browser-cache"
            profile_path = state_root / "browser-profile"
            browser_state_path = state_root / "browser-state"
            for path in (cache_path, profile_path, browser_state_path):
                path.mkdir(parents=True, exist_ok=True)
            self.diagnostics.browser_cache_path = cache_path
            self.diagnostics.browser_profile_path = profile_path
            self.diagnostics.browser_state_path = browser_state_path
            # A persistent context gives every run an explicit, private profile.
            # It is supported by all Playwright browser types and avoids relying
            # on the host's default profile/state directories.
            self._context = browser_type.launch_persistent_context(
                user_data_dir=str(profile_path),
                env={"XDG_CACHE_HOME": str(cache_path)},
            )
            self._page = self._context.new_page()
            self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self.diagnostics.browser_name = browser_name
            self.diagnostics.browser_version = self._context.browser.version if self._context.browser else None
            self.diagnostics.browser_reason = None
            self._page.goto(target)
            return self._page
        except ImportError as exc:
            self.diagnostics.browser_reason = "Playwright is not installed"
            self.diagnostics.files["screenshot.png"] = {"path": str(self.diagnostics.artifact_dir / "screenshot.png"), "available": False, "reason": self.diagnostics.browser_reason}
            self.diagnostics.files["trace.zip"] = {"path": str(self.diagnostics.artifact_dir / "trace.zip"), "available": False, "reason": self.diagnostics.browser_reason}
            self.diagnostics.save()
            raise UiServerError("Browser capability is unavailable", classification=CAPABILITY_FAILURE, diagnostics=self.diagnostics, cause=exc) from exc
        except Exception as exc:
            self.diagnostics.browser_reason = f"browser setup failed: {type(exc).__name__}"
            self.diagnostics.save()
            raise UiServerError("Browser capability setup failed", classification=CAPABILITY_FAILURE, diagnostics=self.diagnostics, cause=exc) from exc

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("browser has not started")
        return self._page

    @property
    def browser_context(self):
        if self._context is None:
            raise RuntimeError("browser has not started")
        return self._context

    def _close_browser(self, failed: bool) -> None:
        screenshot = self.diagnostics.artifact_dir / "screenshot.png"
        trace = self.diagnostics.artifact_dir / "trace.zip"
        if self._context is None and self.diagnostics.browser_reason is None:
            self.diagnostics.browser_reason = "browser context was not created"
        if self._page is not None and failed:
            try:
                self._page.screenshot(path=str(screenshot), full_page=True)
            except Exception as exc:
                self._file_status("screenshot.png", screenshot, reason=f"capture failed: {type(exc).__name__}")
        elif self._page is not None:
            self._file_status("screenshot.png", screenshot, reason="success retention policy")
        else:
            self._file_status("screenshot.png", screenshot, reason=self.diagnostics.browser_reason)
        if self._context is not None:
            try:
                self._context.tracing.stop(path=str(trace))
            except Exception as exc:
                self._file_status("trace.zip", trace, reason=f"capture failed: {type(exc).__name__}")
        else:
            self._file_status("trace.zip", trace, reason=self.diagnostics.browser_reason)
        self._file_status("screenshot.png", screenshot, reason=self.diagnostics.files.get("screenshot.png", {}).get("reason") if "screenshot.png" in self.diagnostics.files else None)
        self._file_status("trace.zip", trace, reason=self.diagnostics.files.get("trace.zip", {}).get("reason") if "trace.zip" in self.diagnostics.files else None)
        resources = (self._context, self._browser, self._playwright)
        for resource in resources:
            if resource is not None:
                try:
                    resource.close() if resource is not self._playwright else resource.stop()
                except Exception as exc:
                    self.diagnostics.readiness["browser_teardown_error"] = type(exc).__name__
        self._context = self._browser = self._playwright = self._page = None

    def _finalize_artifacts(self, outcome: str) -> None:
        self.diagnostics.outcome = outcome
        self.diagnostics.retention_status = "retained" if outcome == "failure" or self.retention == "always" else "cleanup-pending"
        self._file_status("server.stdout.log", self.diagnostics.stdout_path)
        self._file_status("server.stderr.log", self.diagnostics.stderr_path)
        self._close_browser(outcome == "failure")
        self.diagnostics.save()
        if outcome == "success" and self.retention != "always":
            for path in (self.diagnostics.stdout_path, self.diagnostics.stderr_path, self.diagnostics.state_root):
                if path and path.exists():
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
            self.diagnostics.retention_status = "deleted-transient"
            self.diagnostics.save()
        print(f"Browser artifacts: {self.diagnostics.artifact_dir}")

    def _is_bind_conflict(self) -> bool:
        if not self.diagnostics.stderr_path.exists():
            return False
        text = self.diagnostics.stderr_path.read_text(encoding="utf-8", errors="replace").lower()
        return "eaddrinuse" in text or "address already in use" in text

    def _cleanup_owned_process(self) -> None:
        """Stop only the process group belonging to the current attempt."""
        if self.process is None:
            self._close_streams()
            return
        pgid = self.diagnostics.pgid
        if self.diagnostics.process_alive_before is None:
            self.diagnostics.process_alive_before = self._alive()
        if pgid is not None and self._group_exists():
            try:
                os.killpg(pgid, signal.SIGTERM)
                self.diagnostics.termination_signals.append("SIGTERM")
            except ProcessLookupError:
                pass
            graceful_deadline = time.monotonic() + self.graceful_timeout
            while time.monotonic() < graceful_deadline:
                if self.process.poll() is not None and not self._group_exists():
                    break
                time.sleep(min(0.01, max(0, graceful_deadline - time.monotonic())))
            if self._group_exists():
                try:
                    os.killpg(pgid, signal.SIGKILL)
                    self.diagnostics.termination_signals.append("SIGKILL")
                except ProcessLookupError:
                    pass
                forced_deadline = time.monotonic() + self.graceful_timeout
                while time.monotonic() < forced_deadline and self._group_exists():
                    time.sleep(min(0.01, max(0, forced_deadline - time.monotonic())))
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
        if self._alive() or self._group_exists():
            self.diagnostics.readiness["teardown_error"] = "owned process group remained after forced cleanup"
            self.diagnostics.classification = CAPABILITY_FAILURE
        self._close_streams()
        self.diagnostics.process_alive_after = self._alive()
        self.diagnostics.process_group_alive_after = self._group_exists()

    def start(self) -> "UiServerFixture":
        self._prepare()
        self.diagnostics.classification = CAPABILITY_FAILURE
        last_error: BaseException | None = None
        for attempt in range(1, self.port_attempts + 1):
            try:
                port = self._port()
            except OSError as exc:
                self.diagnostics.port_attempts = attempt
                self.diagnostics.port_errors.append(f"attempt {attempt}: {exc}")
                self._finalize_artifacts("failure")
                raise UiServerError("Unable to allocate an ephemeral UI port", classification=CAPABILITY_FAILURE,
                                    diagnostics=self.diagnostics, cause=exc) from exc
            self.diagnostics.port_attempts = attempt
            self.diagnostics.assigned_port = port
            self.diagnostics.base_url = f"http://{self._host}:{port}"
            self.diagnostics.readiness_url = f"{self.diagnostics.base_url}{self.readiness_path}"
            self.diagnostics.command = self._argv(port)
            self.diagnostics.save()
            try:
                # Each attempt gets a fresh stream so bind-conflict detection
                # cannot mistake a prior attempt's stderr for the current one.
                self._stdout = self.diagnostics.stdout_path.open("wb")
                self._stderr = self.diagnostics.stderr_path.open("wb")
                child_env = os.environ.copy()
                child_env.update(self.env_overrides)
                self.process = subprocess.Popen(self.diagnostics.command, cwd=self.diagnostics.data_root, env=child_env,
                                                 stdout=self._stdout, stderr=self._stderr, start_new_session=True)
                self.diagnostics.pid = self.process.pid
                self.diagnostics.pgid = os.getpgid(self.process.pid)
                self.diagnostics.process_alive_before = True
                self.diagnostics.started_at = _now()
                self.diagnostics.save()
                self._wait_ready()
                return self
            except (OSError, ValueError) as exc:
                last_error = exc
            except UiServerError as exc:
                last_error = exc
                if not self._is_bind_conflict():
                    self._cleanup_owned_process()
                    self._finalize_artifacts("failure")
                    raise
            if not self._is_bind_conflict() or attempt == self.port_attempts:
                self._cleanup_owned_process()
                self._finalize_artifacts("failure")
                raise UiServerError("UI runner failed to bind after port attempts", classification=CAPABILITY_FAILURE,
                                    diagnostics=self.diagnostics, cause=last_error) from last_error
            self.diagnostics.port_retries += 1
            self.diagnostics.port_errors.append(f"attempt {attempt}: EADDRINUSE")
            self._cleanup_owned_process()
            self.process = None
            self._stdout = self._stderr = None
            self.diagnostics.pid = self.diagnostics.pgid = None
        raise UiServerError("UI runner failed to start", classification=CAPABILITY_FAILURE,
                            diagnostics=self.diagnostics, cause=last_error)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.readiness_timeout
        self.diagnostics.readiness_deadline = datetime.fromtimestamp(time.time() + self.readiness_timeout, timezone.utc).isoformat()
        self.diagnostics.save()
        last_error = "not attempted"
        observed_status: int | None = None
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                self.diagnostics.readiness = {
                    "status": "process_exited",
                    "expected_status": self.expected_readiness_status,
                    "observed_status": observed_status,
                    "last_error": last_error,
                    "elapsed": self.readiness_timeout - max(0, deadline - time.monotonic()),
                }
                self._write_state()
                raise UiServerError("UI runner exited before readiness", classification=CAPABILITY_FAILURE, diagnostics=self.diagnostics)
            try:
                with urllib.request.urlopen(self.diagnostics.readiness_url, timeout=min(1.0, self.readiness_interval + 0.5)) as response:
                    status = response.status
                observed_status = status
                if status == self.expected_readiness_status:
                    self.diagnostics.classification = None
                    self.diagnostics.readiness = {
                        "status": status,
                        "expected_status": self.expected_readiness_status,
                        "observed_status": status,
                        "ready_at": _now(),
                    }
                    self.diagnostics.save()
                    return
                last_error = f"unexpected HTTP status {status}"
            except urllib.error.HTTPError as exc:
                observed_status = exc.code
                if observed_status == self.expected_readiness_status:
                    self.diagnostics.classification = None
                    self.diagnostics.readiness = {
                        "status": observed_status,
                        "expected_status": self.expected_readiness_status,
                        "observed_status": observed_status,
                        "ready_at": _now(),
                    }
                    self.diagnostics.save()
                    return
                last_error = f"unexpected HTTP status {observed_status}"
            except (urllib.error.URLError, OSError) as exc:
                last_error = str(exc)
            time.sleep(self.readiness_interval)
        self.diagnostics.readiness = {
            "status": "timeout",
            "expected_status": self.expected_readiness_status,
            "observed_status": observed_status,
            "last_error": last_error,
            "deadline": _now(),
        }
        self._write_state()
        raise UiServerError("UI readiness timed out", classification=CAPABILITY_FAILURE, diagnostics=self.diagnostics)

    def teardown(self, *, outcome: str | None = None) -> None:
        if outcome is None:
            outcome = "failure" if self.diagnostics.classification == CAPABILITY_FAILURE else "success"
        self._cleanup_owned_process()
        self.diagnostics.stopped_at = _now()
        self._write_state()
        if self.diagnostics.process_alive_after or self.diagnostics.process_group_alive_after:
            outcome = "failure"
        self._finalize_artifacts(outcome)

    def _close_streams(self) -> None:
        for stream in (self._stdout, self._stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self) -> "UiServerFixture":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.teardown(outcome="failure" if exc_type else "success")
        return False


UIProcessFixture = UiServerFixture
