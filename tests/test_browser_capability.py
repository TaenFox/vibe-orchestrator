from __future__ import annotations

import json
import tomllib
import urllib.request
from pathlib import Path

import pytest

from tests.ui_server_fixture import UiServerFixture
from tests.test_ui_server_fixture import command

ROOT = Path(__file__).parents[1]


def _local_bind_available() -> bool:
    import socket
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


def test_browser_capability_is_optional_and_version_bounded():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert project["project"]["dependencies"] == [
        "PyYAML>=6.0",
        "Starlette>=0.37,<1",
        "Uvicorn>=0.30,<1",
    ]
    assert project["project"]["optional-dependencies"]["browser"] == [
        "playwright>=1.49,<2",
        "pytest-playwright>=0.7,<1",
    ]


def test_browser_marker_and_chromium_setup_are_documented():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    readme = (ROOT / "README.md").read_text()

    assert any(marker.startswith("browser:") for marker in project["tool"]["pytest"]["ini_options"]["markers"])
    assert "pip install -e '.[dev,browser]'" in readme
    assert "python -m playwright install chromium" in readme


@pytest.mark.skipif(not _local_bind_available(), reason="local loopback binding is unavailable")
def test_explicit_parallel_runs_prove_server_and_negative_state_isolation(tmp_path: Path):
    first = UiServerFixture(command(marker="first"), project_root=tmp_path, test_id="isolation")
    second = UiServerFixture(command(marker="second"), project_root=tmp_path, test_id="isolation")
    first_marker = first.diagnostics.state_root / "marker.txt"
    second_marker = second.diagnostics.state_root / "marker.txt"
    try:
        first.start()
        second.start()
        assert first.diagnostics.run_id != second.diagnostics.run_id
        assert first.diagnostics.artifact_dir.resolve() != second.diagnostics.artifact_dir.resolve()
        assert first.data_root.resolve() != second.data_root.resolve()
        assert first.diagnostics.state_root.resolve() != second.diagnostics.state_root.resolve()
        assert first.port != second.port
        assert urllib.request.urlopen(first.base_url).read() == b"first"
        assert urllib.request.urlopen(second.base_url).read() == b"second"

        first_marker.write_text(first.diagnostics.run_id, encoding="utf-8")
        second_marker.write_text(second.diagnostics.run_id, encoding="utf-8")
        assert not (first.diagnostics.state_root / "../marker.txt").resolve().exists()
        assert second_marker.read_text(encoding="utf-8") != first_marker.read_text(encoding="utf-8")
        second_before = second_marker.read_text(encoding="utf-8")
        attempted_cross_write = first.diagnostics.state_root / second_marker.name
        attempted_cross_write.write_text("cross-run", encoding="utf-8")
        assert attempted_cross_write.resolve().is_relative_to(first.diagnostics.state_root.resolve())
        assert not attempted_cross_write.resolve().is_relative_to(second.diagnostics.state_root.resolve())
        assert second_marker.read_text(encoding="utf-8") == second_before
    finally:
        second.teardown()
        first.teardown()
    assert first.diagnostics.process_alive_after is False
    assert first.diagnostics.process_group_alive_after is False
    assert second.diagnostics.process_alive_after is False
    assert second.diagnostics.process_group_alive_after is False


@pytest.mark.browser
@pytest.mark.skipif(not _local_bind_available(), reason="local loopback binding is unavailable")
def test_parallel_browser_contexts_have_private_profile_paths(tmp_path: Path):
    first = UiServerFixture(command(marker="first"), project_root=tmp_path, test_id="browser-isolation")
    second = UiServerFixture(command(marker="second"), project_root=tmp_path, test_id="browser-isolation")
    try:
        first.start()
        second.start()
        try:
            first.start_browser()
            second.start_browser()
        except Exception as exc:
            pytest.skip(f"Playwright/browser binary unavailable: {type(exc).__name__}")
        assert first.browser_context is not second.browser_context
        assert first.diagnostics.browser_cache_path.is_relative_to(first.diagnostics.state_root)
        assert second.diagnostics.browser_cache_path.is_relative_to(second.diagnostics.state_root)
        assert first.diagnostics.browser_profile_path != second.diagnostics.browser_profile_path
        assert first.diagnostics.browser_state_path != second.diagnostics.browser_state_path
    finally:
        second.teardown()
        first.teardown()


def test_manifest_is_atomic_and_records_unavailable_browser_artifacts(tmp_path: Path):
    fixture = UiServerFixture([], project_root=tmp_path, test_id="manifest")
    fixture._finalize_artifacts("failure")

    manifest = fixture.diagnostics.manifest_path
    assert manifest is not None and manifest.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["artifact_root"] == str(fixture.diagnostics.artifact_dir)
    assert data["worktree"] == str(tmp_path.resolve())
    assert data["files"]["screenshot.png"]["available"] is False
    assert data["files"]["screenshot.png"]["reason"]
    assert data["files"]["trace.zip"]["reason"]
    assert str(fixture.diagnostics.artifact_dir) in data["artifact_root"]
    assert not list(manifest.parent.glob("*.tmp"))


def test_success_retention_deletes_transient_files_but_keeps_manifest(tmp_path: Path):
    fixture = UiServerFixture([], project_root=tmp_path, retention="failure")
    fixture.diagnostics.stdout_path.write_text("stdout", encoding="utf-8")
    fixture.diagnostics.stderr_path.write_text("stderr", encoding="utf-8")
    fixture._finalize_artifacts("success")

    assert fixture.diagnostics.manifest_path.exists()
    assert not fixture.diagnostics.stdout_path.exists()
    assert not fixture.diagnostics.stderr_path.exists()
    assert not fixture.diagnostics.state_root.exists()
    assert json.loads(fixture.diagnostics.manifest_path.read_text())["retention"]["status"] == "deleted-transient"
