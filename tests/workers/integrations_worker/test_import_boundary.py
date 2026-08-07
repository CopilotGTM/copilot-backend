"""Import-boundary regression tests for the integrations-worker image."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_crm_registry_writer_does_not_import_api_package() -> None:
    """Schema sync must initialize with the packages copied into the worker image."""
    repository_root = Path(__file__).resolve().parents[3]
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys


        class RejectApiImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "src.api" or fullname.startswith("src.api."):
                    raise ModuleNotFoundError("integrations-worker boundary forbids src.api")
                return None


        sys.meta_path.insert(0, RejectApiImports())

        from src.service.integrations.crm.schema.common import registry_writer  # noqa: F401
        """
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
