from __future__ import annotations

"""Example shared tool wrapper for web search (DuckDuckGo via agno).

Agents can import and use this to avoid depending on the underlying library
directly in each agent.
"""
try:
    from agno.tools.duckduckgo import DuckDuckGoTools as _DDG
except Exception:  # pragma: no cover - optional dependency at import time
    _DDG = None


class WebSearch:
    def __init__(self):
        if _DDG is None:  # pragma: no cover
            raise RuntimeError("DuckDuckGo tool not available")
        self._impl = _DDG()

    @property
    def impl(self):
        return self._impl

