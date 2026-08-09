import tomllib
from pathlib import Path

from packaging.requirements import Requirement


def test_light_worker_installs_markdown_renderer() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["optional-dependencies"]["workers-light"]

    assert "markdown" in {Requirement(dependency).name for dependency in dependencies}
