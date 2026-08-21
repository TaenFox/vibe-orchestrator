from __future__ import annotations

import tomllib
from pathlib import Path


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
