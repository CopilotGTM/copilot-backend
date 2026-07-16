"""Search tools for agents.

This package contains tools for searching various data sources:
- internal_knowledge_base: Search organisation documents
- internet_search: Web search via Exa
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .internal_knowledge_base import (
        create_search_internal_knowledge,
        internal_knowledge_base,
    )
    from .internet_search import create_search_internet, internet_search

_EXPORTS = {
    "create_search_internal_knowledge": (
        "src.agents.tools.search.internal_knowledge_base",
        "create_search_internal_knowledge",
    ),
    "internal_knowledge_base": (
        "src.agents.tools.search.internal_knowledge_base",
        "internal_knowledge_base",
    ),
    "create_search_internet": (
        "src.agents.tools.search.internet_search",
        "create_search_internet",
    ),
    "internet_search": (
        "src.agents.tools.search.internet_search",
        "internet_search",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, export_name = _EXPORTS[name]
    module = import_module(module_name)
    return getattr(module, export_name)
