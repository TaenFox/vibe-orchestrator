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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


CAPABILITY_FAILURE = "capability_environment_failure"
PRODUCT_FAILURE = "ui_product_failure"
SCHEMA_VERSION = 1


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
        }

    def save(self) -> None:
        self.metadata_path.write_text(json.dumps(self.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


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
        self._host = host
        self.readiness_path = readiness_path if readiness_path.startswith("/") else f"/{readiness_path}"
        if not isinstance(expected_readiness_status, int):
            raise TypeError("expected_readiness_status must be an integer")
        self.expected_readiness_status = expected_readiness_status
        self.readiness_timeout = readiness_timeout
        self.readiness_interval = readiness_interval
        self.graceful_timeout = graceful_timeout
        self.port_attempts = port_attempts
        self.env_overrides = dict(env or {})
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout = None
        self._stderr = None
        self.diagnostics = self._placeholder_diagnostics()
        self.diagnostics.host = host
        self.diagnostics.expected_readiness_status = expected_readiness_status
        self.diagnostics.environment_overrides = {
            key: "<redacted>" if any(token in key.upper() for token in ("SECRET", "TOKEN", "PASSWORD", "KEY")) else value
            for key, value in self.env_overrides.items()
        }

    def _placeholder_diagnostics(self) -> UiServerDiagnostics:
        artifact_dir = Path(tempfile.mkdtemp(prefix="vibe-ui-fixture-"))
        data_root = artifact_dir / "project"
        data_root.mkdir()
        return UiServerDiagnostics(artifact_dir, artifact_dir / "metadata.json", artifact_dir / "stdout.log", artifact_dir / "stderr.log", data_root)

    def _prepare(self) -> None:
        if self.source_root:
            shutil.copytree(self.source_root, self.diagnostics.data_root, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    def _port(self) -> int:
        last: OSError | None = None
        for _ in range(self.port_attempts):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind((self._host, 0))
                    return int(sock.getsockname()[1])
            except OSError as exc:
                last = exc
        raise UiServerError("Unable to allocate an ephemeral UI port", classification=CAPABILITY_FAILURE, diagnostics=self.diagnostics, cause=last)

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

    def start(self) -> "UiServerFixture":
        self._prepare()
        port = self._port()
        self.diagnostics.assigned_port = port
        self.diagnostics.base_url = f"http://{self._host}:{port}"
        self.diagnostics.readiness_url = f"{self.diagnostics.base_url}{self.readiness_path}"
        self.diagnostics.command = self._argv(port)
        self.diagnostics.classification = CAPABILITY_FAILURE
        self.diagnostics.save()
        try:
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
        except (OSError, ValueError) as exc:
            self._write_state()
            self.teardown()
            raise UiServerError("UI runner failed to start", classification=CAPABILITY_FAILURE, diagnostics=self.diagnostics, cause=exc) from exc
        try:
            self._wait_ready()
        except UiServerError:
            self.teardown()
            raise
        return self

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

    def teardown(self) -> None:
        if self.process is None:
            self._close_streams()
            self._write_state()
            return
        if self.diagnostics.process_alive_before is None:
            self.diagnostics.process_alive_before = self._alive()
        pgid = self.diagnostics.pgid
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
        self.diagnostics.stopped_at = _now()
        self._write_state()

    def _close_streams(self) -> None:
        for stream in (self._stdout, self._stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self) -> "UiServerFixture":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.teardown()
        return False


UIProcessFixture = UiServerFixture
