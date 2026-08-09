import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement


@pytest.mark.parametrize("worker_extra", ["workers-light", "full"])
def test_chat_worker_images_install_markdown_renderer(worker_extra: str) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["optional-dependencies"][worker_extra]

    assert "markdown" in {Requirement(dependency).name for dependency in dependencies}
