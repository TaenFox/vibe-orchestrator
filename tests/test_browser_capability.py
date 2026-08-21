from __future__ import annotations

import json
import tomllib
from pathlib import Path

from tests.ui_server_fixture import UiServerFixture

ROOT = Path(__file__).parents[1]


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


def test_browser_runs_allocate_independent_artifact_and_state_roots(tmp_path: Path):
    first = UiServerFixture([], project_root=tmp_path, test_id="isolation")
    second = UiServerFixture([], project_root=tmp_path, test_id="isolation")

    assert first.diagnostics.artifact_dir != second.diagnostics.artifact_dir
    assert first.diagnostics.state_root != second.diagnostics.state_root
    assert first.diagnostics.artifact_dir.parent == second.diagnostics.artifact_dir.parent
    assert first.diagnostics.artifact_dir.parent.parent == tmp_path / ".vibe" / "browser-artifacts"
    assert first.diagnostics.worktree == tmp_path.resolve()


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
