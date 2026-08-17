from pathlib import Path

import pytest

from vibe_orchestrator import config


def test_load_workflow_requires_explicit_agent_execution_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "broken.yaml").write_text(
        """id: broken
stages:
  - id: review
    kind: agent
    prompt: broken/review.md
    model: gpt-5.6-luna
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "workflow_dir", lambda: workflow_dir)

    with pytest.raises(ValueError, match="Agent stage 'review' must declare reasoning_effort"):
        config.load_workflow("broken")
