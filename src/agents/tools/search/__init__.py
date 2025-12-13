"""Search tools for agents.

This package contains tools for searching various data sources:
- internal_knowledge_base: Search organisation documents
- internet_search: Web search via DuckDuckGo
"""

from .internal_knowledge_base import (
    create_search_internal_knowledge,
    internal_knowledge_base,
)
from .internet_search import create_search_internet, internet_search

__all__ = [
    "create_search_internal_knowledge",
    "internal_knowledge_base",
    "create_search_internet",
    "internet_search",
]
