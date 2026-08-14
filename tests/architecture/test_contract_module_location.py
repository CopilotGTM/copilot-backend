"""Keep shared agent contracts out of the HTTP package.

API routers, services, agents, and workers all consume these models. The
canonical modules therefore live under ``src.contracts`` without legacy API
aliases that can reintroduce worker-to-API dependencies.
"""

from __future__ import annotations

from importlib.util import find_spec


def test_legacy_api_agent_contract_modules_are_removed() -> None:
    """Prevent callers from restoring the deleted API compatibility modules."""

    assert find_spec("src.api.schemas.agent_models") is None
    assert find_spec("src.api.schemas.automation_trigger_models") is None
