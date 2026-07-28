"""Regression tests for authenticated user-level task reads."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.agents.context import AgentContext


@pytest.mark.asyncio
async def test_list_my_tasks_uses_authenticated_identity_and_local_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The personal tool derives identity and today's UTC bounds from context."""
    module = importlib.import_module("src.agents.tools.tasks.list_my_tasks")
    context = AgentContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        user_email="admin@example.com",
        metadata={
            "user_timezone": "Asia/Calcutta",
            "current_datetime": "2026-07-27T18:30:00Z",
        },
    )

    @asynccontextmanager
    async def acquire_session():
        """Provide the tool a session without opening a tenant database."""
        yield object()

    context.acquire_session = acquire_session  # type: ignore[method-assign]
    task = SimpleNamespace(
        activity_id=uuid4(),
        subject="Call customer",
        body=None,
        status="NOT_STARTED",
        priority="HIGH",
        due_date=None,
        due_at=datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
        owner_name="Admin",
        owner_email=context.user_email,
        source="USER",
        integration_id=None,
    )
    record = SimpleNamespace(task=task, deal_name="Renewal")

    class FakeTaskReadService:
        """Capture the canonical read-service arguments at the tool boundary."""

        instances: list[FakeTaskReadService] = []

        def __init__(self, session, organisation_id, access_actor):
            self.session = session
            self.organisation_id = organisation_id
            self.access_actor = access_actor
            self.calls: list[dict] = []
            self.instances.append(self)

        async def list_tasks(self, **kwargs):
            """Return one visible assignment while retaining the requested filters."""
            self.calls.append(kwargs)
            return [record], 1

    role_resolver = AsyncMock(
        side_effect=AssertionError("ListMyTasks must use immutable context identity directly")
    )
    monkeypatch.setattr(module, "resolve_agent_access_actor", role_resolver, raising=False)
    monkeypatch.setattr(module, "TaskReadService", FakeTaskReadService)

    result = await module.list_my_tasks(context=context, due_today=True)

    service = FakeTaskReadService.instances[0]
    call = service.calls[0]
    assert call["scope"] == "assigned_to_me"
    assert call["current_user_id"] == context.user_id
    assert call["current_user_email"] == context.user_email
    assert call["due_from"].isoformat() == "2026-07-27T18:30:00+00:00"
    assert call["due_to"].isoformat() == "2026-07-28T18:29:59.999999+00:00"
    assert call["due_local_date"] == date(2026, 7, 28)
    assert result["task_count"] == 1
    assert result["tasks"][0]["deal_name"] == "Renewal"
    assert result["tasks"][0]["due_date"] == "2026-07-28T09:30:00+00:00"
    assert service.access_actor.user_id == context.user_id
    assert service.access_actor.normalized_email == context.user_email
    assert service.access_actor.is_admin is False
    role_resolver.assert_not_awaited()
