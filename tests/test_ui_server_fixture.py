from __future__ import annotations

import json
import shutil
import socket
import sys
import textwrap
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

import pytest

from tests.ui_server_fixture import CAPABILITY_FAILURE, UiServerError, UiServerFixture


def _local_bind_available() -> bool:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _local_bind_available(), reason="local loopback binding is unavailable")


RUNNER = textwrap.dedent(
    """
    import http.server, os, signal, sys, time
    from urllib.parse import parse_qs, urlparse
    root, host, port, delay, ignore, status, marker, conflict = sys.argv[1:]
    if ignore == '1': signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if delay == 'exit': raise SystemExit(7)
    time.sleep(float(delay))
    if conflict == '1':
        raise OSError(98, 'Address already in use')
    class Handler(http.server.BaseHTTPRequestHandler):
        def _state_namespace_allowed(self):
            query = parse_qs(urlparse(self.path).query)
            requested = query.get('namespace', [os.environ['VIBE_UI_STATE_NAMESPACE']])[0]
            return requested == os.environ['VIBE_UI_STATE_NAMESPACE']

        def do_GET(self):
            body = marker.encode()
            if self.path == '/state':
                with open(os.path.join(os.environ['VIBE_UI_STATE_ROOT'], 'state-marker.txt'), encoding='utf-8') as state:
                    body = state.read().encode()
            elif urlparse(self.path).path == '/state':
                if not self._state_namespace_allowed():
                    self.send_response(403); self.end_headers(); return
                with open(os.path.join(os.environ['VIBE_UI_STATE_ROOT'], 'state-marker.txt'), encoding='utf-8') as state:
                    body = state.read().encode()
            self.send_response(int(status)); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            if urlparse(self.path).path != '/state' or not self._state_namespace_allowed():
                self.send_response(403); self.end_headers(); return
            length = int(self.headers.get('Content-Length', '0'))
            value = self.rfile.read(length)
            with open(os.path.join(os.environ['VIBE_UI_STATE_ROOT'], 'state-marker.txt'), 'wb') as state:
                state.write(value)
            self.send_response(204); self.end_headers()
        def log_message(self, *args): pass
    server = http.server.ThreadingHTTPServer((host, int(port)), Handler)
    signal.signal(signal.SIGTERM, lambda *_: raise_exit())
    def raise_exit(): raise SystemExit(0)
    open('runner-cwd.txt', 'w').write(root)
    server.serve_forever()
    """
)


def command(delay="0", ignore="0", status="200", marker="ready", conflict="0"):
    return [sys.executable, "-c", RUNNER, "{project_root}", "{host}", "{port}", delay, ignore, status, marker, conflict]


def test_starts_on_ephemeral_port_and_persists_metadata(tmp_path: Path):
    fixture = UiServerFixture(command(), project_root=tmp_path, readiness_timeout=2)
    with fixture as server:
        assert server.port > 0
        assert server.base_url.endswith(f":{server.port}")
        assert server.data_root != tmp_path.resolve()
        assert urllib.request.urlopen(server.base_url).status == 200
        assert (server.data_root / "runner-cwd.txt").read_text() == str(server.data_root)
    metadata = json.loads(server.diagnostics.metadata_path.read_text())
    assert metadata["requested_port"] == 0
    assert metadata["assigned_port"] == server.port
    assert urlsplit(metadata["base_url"]).port == server.port
    with socket.create_connection((server.host, server.port), timeout=1):
        pass
    assert metadata["last_attempt_port"] == server.port
    assert metadata["pid"] and metadata["pgid"]
    assert metadata["process_alive_after"] is False
    assert metadata["process_group_alive_after"] is False
    assert metadata["expected_readiness_status"] == 200


def test_readiness_accepts_configured_status_and_persists_observation(tmp_path: Path):
    fixture = UiServerFixture(command(status="204"), project_root=tmp_path, expected_readiness_status=204)
    with fixture:
        pass
    metadata = json.loads(fixture.diagnostics.metadata_path.read_text())
    assert metadata["expected_readiness_status"] == 204
    assert metadata["readiness"]["observed_status"] == 204


def test_readiness_mismatch_times_out_with_expected_and_observed_status(tmp_path: Path):
    fixture = UiServerFixture(
        command(status="204"), project_root=tmp_path, expected_readiness_status=200,
        readiness_timeout=0.15, readiness_interval=0.02,
    )
    with pytest.raises(UiServerError):
        fixture.start()
    readiness = fixture.diagnostics.readiness
    assert readiness["expected_status"] == 200
    assert readiness["observed_status"] == 204


@pytest.mark.parametrize("runner", [command("exit"), command("5")])
def test_startup_and_readiness_failures_teardown(runner, tmp_path: Path):
    fixture = UiServerFixture(runner, project_root=tmp_path, readiness_timeout=0.2, readiness_interval=0.02, graceful_timeout=0.1)
    with pytest.raises(UiServerError) as caught:
        fixture.start()
    assert caught.value.classification == CAPABILITY_FAILURE
    assert fixture.process.poll() is not None
    assert fixture.diagnostics.metadata_path.exists()
    assert fixture.diagnostics.stdout_path.exists()
    assert fixture.diagnostics.stderr_path.exists()


def test_missing_runner_is_capability_failure(tmp_path: Path):
    fixture = UiServerFixture(["/definitely/missing/vibe-ui"], project_root=tmp_path)
    with pytest.raises(UiServerError) as caught:
        fixture.start()
    assert caught.value.classification == CAPABILITY_FAILURE
    assert caught.value.cause is not None


def test_preparation_failure_retains_failure_bundle_and_original_cause(tmp_path: Path, monkeypatch):
    fixture = UiServerFixture(command(), project_root=tmp_path, test_id="prepare-failure")
    original = OSError("copy failed")

    def fail_copytree(*args, **kwargs):
        raise original

    monkeypatch.setattr(shutil, "copytree", fail_copytree)

    with pytest.raises(UiServerError) as caught:
        fixture.start()

    assert caught.value.classification == CAPABILITY_FAILURE
    assert caught.value.cause is original
    assert fixture.diagnostics.manifest_path.exists()
    manifest = json.loads(fixture.diagnostics.manifest_path.read_text(encoding="utf-8"))
    assert manifest["outcome"] == "failure"
    assert manifest["preparation"]["status"] == "failed"
    assert manifest["preparation"]["error_type"] == "OSError"
    assert fixture.diagnostics.artifact_dir.exists()


def test_sigkill_fallback_is_limited_to_owned_group(tmp_path: Path):
    unrelated = __import__("subprocess").Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    fixture = UiServerFixture(command(ignore="1"), project_root=tmp_path, readiness_timeout=2, graceful_timeout=0.05)
    try:
        with fixture:
            pass
        assert "SIGTERM" in fixture.diagnostics.termination_signals
        assert "SIGKILL" in fixture.diagnostics.termination_signals
        assert unrelated.poll() is None
        assert fixture.diagnostics.process_alive_after is False
        assert fixture.diagnostics.process_group_alive_after is False
    finally:
        unrelated.terminate()
        unrelated.wait()


def test_cleanup_does_not_delete_external_state_root(tmp_path: Path):
    fixture = UiServerFixture([], project_root=tmp_path, retention="failure")
    external = tmp_path / "external-state"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    fixture.diagnostics.state_root = external

    fixture._finalize_artifacts("success")

    assert sentinel.exists()
    manifest = json.loads(fixture.diagnostics.manifest_path.read_text(encoding="utf-8"))
    assert manifest["retention"]["status"] == "cleanup-safety-failed"
    assert any("state_root" in error for error in manifest["cleanup"]["errors"])


def test_cleanup_does_not_delete_artifact_dir_outside_configured_base(tmp_path: Path):
    artifact_base = tmp_path / "configured-artifacts"
    fixture = UiServerFixture([], project_root=tmp_path, artifact_base=artifact_base, retention="failure")
    external_artifact_dir = tmp_path / "external-artifacts"
    external_artifact_dir.mkdir()
    sentinel = external_artifact_dir / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    fixture.diagnostics.artifact_dir = external_artifact_dir

    fixture._finalize_artifacts("success")

    assert sentinel.exists()
    manifest = json.loads(fixture.diagnostics.manifest_path.read_text(encoding="utf-8"))
    assert manifest["retention"]["status"] == "cleanup-safety-failed"
    assert any("artifact_dir" in error for error in manifest["cleanup"]["errors"])


def test_bind_conflict_retries_with_new_factually_ready_port(tmp_path: Path):
    attempts = 0

    def runner(project, host, port):
        nonlocal attempts
        attempts += 1
        return command(marker="retry-ok", conflict="1" if attempts == 1 else "0")

    fixture = UiServerFixture(runner, project_root=tmp_path, port_attempts=2, readiness_timeout=1)
    with fixture as server:
        assert urllib.request.urlopen(server.base_url).read() == b"retry-ok"
    metadata = json.loads(fixture.diagnostics.manifest_path.read_text())
    assert attempts == 2
    assert metadata["port_attempts"] == 2
    assert metadata["port_retries"] == 1
    assert metadata["port_errors"] == ["attempt 1: EADDRINUSE"]
    assert metadata["assigned_port"] == server.port
    assert metadata["base_url"].endswith(f":{server.port}")
    assert metadata["readiness"]["observed_status"] == 200
    with socket.create_connection((server.host, server.port), timeout=1):
        pass
    assert metadata["process_alive_after"] is False
    assert metadata["process_group_alive_after"] is False


def test_exhausted_bind_retries_persist_diagnostics(tmp_path: Path):
    fixture = UiServerFixture(command(conflict="1"), project_root=tmp_path, port_attempts=2, readiness_timeout=1)
    with pytest.raises(UiServerError) as caught:
        fixture.start()
    assert caught.value.classification == CAPABILITY_FAILURE
    assert fixture.diagnostics.port_attempts == 2
    assert fixture.diagnostics.port_retries == 1
    assert len(fixture.diagnostics.port_errors) == 2
    assert fixture.diagnostics.port_errors == ["attempt 1: EADDRINUSE", "attempt 2: EADDRINUSE"]
    assert fixture.diagnostics.assigned_port is None
    assert fixture.diagnostics.last_attempt_port is not None
    assert "Address already in use" in fixture.diagnostics.stderr_path.read_text()
    assert fixture.diagnostics.metadata_path.exists()
