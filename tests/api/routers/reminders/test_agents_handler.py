from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.reminders import agents_handler
from src.api.routers.reminders.agents_handler import create_agent, get_agent, list_agents
from src.contracts.reminders import AgentCreateRequest
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
)
from src.service.reminders.reminder_service import ReminderNotFoundError


def _future_trigger() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


class _FakeReminderService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return None

    async def list_reminders(self, **kwargs):
        self.calls.append(("list_reminders", kwargs))
        return [], 0

    async def get(self, reminder_id, **kwargs):
        self.calls.append(("get", {"reminder_id": reminder_id, **kwargs}))
        if kwargs.get("reminder_type") != ReminderType.AGENTIC:
            raise ReminderNotFoundError(reminder_id)
        return None


def test_agent_create_request_rejects_static_message_content() -> None:
    with pytest.raises(ValidationError):
        AgentCreateRequest.model_validate(
            {
                "title": "Deal health check",
                "agent_prompt": "Summarize risks for the deal",
                "message_content": "Static message",
                "trigger_time": _future_trigger(),
                "timezone": "UTC",
                "recurrence": ReminderRecurrence.NONE,
                "delivery_channel": "teams",
            }
        )


def test_agent_create_request_requires_deal_id() -> None:
    with pytest.raises(ValidationError):
        AgentCreateRequest.model_validate(
            {
                "title": "Deal health check",
                "agent_prompt": "Summarize risks for the deal",
                "trigger_time": _future_trigger(),
                "timezone": "UTC",
                "recurrence": ReminderRecurrence.NONE,
                "delivery_channel": "teams",
            }
        )

    with pytest.raises(ValidationError):
        AgentCreateRequest.model_validate(
            {
                "title": "Deal health check",
                "agent_prompt": "Summarize risks for the deal",
                "deal_id": None,
                "trigger_time": _future_trigger(),
                "timezone": "UTC",
                "recurrence": ReminderRecurrence.NONE,
                "delivery_channel": "teams",
            }
        )


@pytest.mark.asyncio
async def test_create_agent_always_creates_agentic_reminder(monkeypatch: pytest.MonkeyPatch):
    service = _FakeReminderService()

    async def fake_resolve_delivery_context(*args, **kwargs):
        return {"service_url": "https://service", "conversation_id": "conv-123"}

    monkeypatch.setattr(agents_handler, "_resolve_delivery_context", fake_resolve_delivery_context)

    deal_id = uuid4()
    body = AgentCreateRequest(
        title="Deal health check",
        agent_prompt="Summarize risks for the deal",
        deal_id=deal_id,
        trigger_time=_future_trigger(),
        timezone="UTC",
        recurrence=ReminderRecurrence.NONE,
        delivery_channel="teams",
    )

    await create_agent(
        body=body,
        response=FastAPIResponse(),
        user=type("User", (), {"organisation_id": uuid4(), "user_id": uuid4()})(),
        master_session=object(),
        service=service,
    )

    assert service.calls[0][1]["reminder_type"] == ReminderType.AGENTIC
    assert service.calls[0][1]["message_content"] is None
    assert service.calls[0][1]["deal_id"] == deal_id


@pytest.mark.asyncio
async def test_list_agents_applies_agentic_type_filter() -> None:
    service = _FakeReminderService()

    await list_agents(
        response=FastAPIResponse(),
        service=service,
        query=agents_handler.ReminderListQueryParams(status=ReminderStatus.ACTIVE.value),
    )

    assert service.calls[0][1]["reminder_type"] == ReminderType.AGENTIC


@pytest.mark.asyncio
async def test_get_agent_uses_agentic_type_boundary() -> None:
    service = _FakeReminderService()
    reminder_id = uuid4()

    await get_agent(
        reminder_id=reminder_id,
        response=FastAPIResponse(),
        service=service,
    )

    assert service.calls[0] == (
        "get",
        {"reminder_id": reminder_id, "reminder_type": ReminderType.AGENTIC},
    )
