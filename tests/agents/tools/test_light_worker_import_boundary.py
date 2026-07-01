"""Import-boundary checks for chat worker tools.

The light-worker Docker image intentionally omits ``src/api``. Agent tools used
by chat workers must therefore import shared contracts from non-API modules.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from uuid import UUID

import pytest


class _BlockSrcApiImports(MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object | None,
        target: object | None = None,
    ) -> ModuleSpec | None:
        if fullname == "src.api" or fullname.startswith("src.api."):
            raise ModuleNotFoundError("No module named 'src.api'")
        return None


@contextmanager
def _without_src_api():
    blocked = _BlockSrcApiImports()
    cached_api_modules = {
        name: module for name, module in sys.modules.items() if name.startswith("src.api")
    }
    for name in cached_api_modules:
        sys.modules.pop(name, None)

    sys.meta_path.insert(0, blocked)
    try:
        yield
    finally:
        sys.meta_path.remove(blocked)
        sys.modules.update(cached_api_modules)


@pytest.mark.parametrize(
    "module_name",
    [
        "src.agents.tools.reminders.create_reminder",
        "src.agents.tools.tasks.create_task",
        "src.agents.tools.crm.create_deal",
        "src.agents.tools.crm.create_lead",
        "src.agents.tools.crm.create_account",
        "src.agents.tools.crm.create_contact",
        "src.agents.tools.crm.add_deal_note",
    ],
)
def test_chat_write_tools_import_without_api_package(module_name: str):
    with _without_src_api():
        importlib.import_module(module_name)


def test_general_agent_tools_load_without_api_package():
    from src.agents.context import AgentContext
    from src.agents.tools.registry import get_tools_for_agent

    context = AgentContext(
        organisation_id=UUID("1982306b-12a9-4e4b-9e6d-6996df7b1698"),
        user_id=UUID("5be25739-f5cd-4040-af0a-b5719e9c03fe"),
        channel="teams",
    )

    with _without_src_api():
        tools = get_tools_for_agent("GeneralAgent", context, lambda: None)

    assert tools
