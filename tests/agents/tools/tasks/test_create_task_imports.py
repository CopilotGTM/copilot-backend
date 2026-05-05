from __future__ import annotations

import subprocess
import sys


def test_task_tool_and_write_service_import_without_resend_installed() -> None:
    code = r"""
import importlib.abc
import sys

class BlockResend(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "resend":
            raise ModuleNotFoundError("No module named 'resend'")
        return None

sys.meta_path.insert(0, BlockResend())

import src.agents.tools.tasks.create_task
import src.service.crm.internal.tasks_write_service
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter command for import isolation.
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
