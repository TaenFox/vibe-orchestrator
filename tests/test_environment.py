from pathlib import Path
import sys

from vibe_orchestrator.environment import EnvironmentSpec, ProjectEnvironment


def test_environment_spec_is_read_from_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        """[tool.vibe.environment]\ninterpreter = \".venv/bin/python\"\nsetup = \"python -m pip install -e .\"\ncheck = \"python -m pytest -q\"\n""",
        encoding="utf-8",
    )

    spec = EnvironmentSpec.from_project(tmp_path)

    assert spec is not None
    assert spec.interpreter == ".venv/bin/python"
    assert spec.setup == "python -m pip install -e ."


def test_project_without_environment_contract_is_noop(tmp_path: Path):
    environment = ProjectEnvironment(tmp_path)

    environment.ensure()

    assert environment.agent_environment() is None
    assert environment.instructions() is None


def test_environment_keeps_virtualenv_path_when_interpreter_is_symlink(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        """[tool.vibe.environment]\ninterpreter = \".venv/bin/python\"\n""",
        encoding="utf-8",
    )
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)

    environment = ProjectEnvironment(tmp_path)
    environment.ensure()

    assert environment.agent_environment()["VIRTUAL_ENV"] == str(tmp_path / ".venv")
