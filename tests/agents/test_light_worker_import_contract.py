"""Import-boundary regression tests for the lightweight chat-worker image."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_general_agent_registration_does_not_import_api_package() -> None:
    """GeneralAgent must initialize with the packages copied into the light-worker image."""
    repository_root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys
        from uuid import uuid4


        class RejectApiImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "src.api" or fullname.startswith("src.api."):
                    raise ModuleNotFoundError("light-worker boundary forbids src.api")
                return None


        sys.meta_path.insert(0, RejectApiImports())

        from src.agents.context import AgentContext
        from src.agents.workflows.general.agent import GeneralAgent

        GeneralAgent(context=AgentContext(organisation_id=uuid4()))
        """
    )

    # The executable and script are fixed test inputs, not user-controlled data.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
