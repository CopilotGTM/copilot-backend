"""Authenticated user setup for CRM agent-tool regression tests.

Tool behavior tests historically mocked only the organisation database. Human
CRM tools now resolve a master user before touching deal data, so this fixture
supplies that independently authenticated actor while dedicated access-policy
tests continue to cover relationship decisions.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm import access as access_module
from src.domain.models.master_tables.user_models import UserAppRole
from src.service.crm.access.record_access_policy import CRMAccessActor


@pytest.fixture(autouse=True)
def authenticated_agent_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep tool tests behind a concrete member identity without using a live master DB."""

    async def resolve_member(context: AgentContext) -> CRMAccessActor:
        assert context.user_id is not None
        return CRMAccessActor(context.user_id, UserAppRole.MEMBER, context.user_email)

    resolver = AsyncMock(side_effect=resolve_member)
    access_guard = AsyncMock(side_effect=lambda _session, _org_id, actor, *_args: actor)
    monkeypatch.setattr(access_module, "resolve_agent_access_actor", resolver)
    monkeypatch.setattr(access_module, "require_agent_deal_access", access_guard)

    # Tool modules bind these functions at import time. Patch every loaded CRM
    # tool boundary; authorization behavior stays in its dedicated policy tests.
    modules = (
        module
        for name, module in sys.modules.items()
        if name.startswith("src.agents.tools.crm.") and isinstance(module, ModuleType)
    )
    for module in modules:
        if hasattr(module, "resolve_agent_access_actor"):
            monkeypatch.setattr(module, "resolve_agent_access_actor", resolver)
        if hasattr(module, "require_agent_deal_access"):
            monkeypatch.setattr(module, "require_agent_deal_access", access_guard)
