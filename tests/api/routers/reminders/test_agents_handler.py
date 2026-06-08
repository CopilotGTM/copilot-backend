from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.reminders import agents_handler
from src.api.routers.reminders.agents_handler import (
    create_agent,
    get_agent,
    get_available_agent_channels,
    list_agents,
    update_agent,
)
from src.contracts.reminders import AgentCreateRequest
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
)
from src.service.channels import OutboundChannelInfo, OutboundDeliveryTarget
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

    async def update(self, reminder_id, updates, **kwargs):
        self.calls.append(("update", {"reminder_id": reminder_id, "updates": updates, **kwargs}))
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
async def test_get_available_agent_channels_uses_outbound_channel_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOutboundChannelService:
        async def list_user_channels(self, master_session, organisation_id, user_id):
            return [
                OutboundChannelInfo(
                    channel="teams",
                    available=True,
                    label="Microsoft Teams",
                    icon_url="/integrations/microsoft-teams.png",
                    mapping_id=uuid4(),
                ),
                OutboundChannelInfo(
                    channel="slack",
                    available=False,
                    label="Slack",
                    icon_url="/integrations/slack.png",
                    mapping_id=None,
                ),
                OutboundChannelInfo(
                    channel="whatsapp",
                    available=True,
                    label="WhatsApp",
                    icon_url="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
                    mapping_id=uuid4(),
                ),
            ]

    monkeypatch.setattr(
        agents_handler,
        "OutboundChannelService",
        FakeOutboundChannelService,
    )

    result = await get_available_agent_channels(
        response=FastAPIResponse(),
        user=type("User", (), {"organisation_id": uuid4(), "user_id": uuid4()})(),
        master_session=object(),
    )

    assert [channel.channel for channel in result.data.channels] == [
        "teams",
        "slack",
        "whatsapp",
    ]
    assert [channel.icon_url for channel in result.data.channels] == [
        "/integrations/microsoft-teams.png",
        "/integrations/slack.png",
        "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
    ]


@pytest.mark.asyncio
async def test_create_agent_always_creates_agentic_reminder(monkeypatch: pytest.MonkeyPatch):
    service = _FakeReminderService()

    class FakeOutboundChannelService:
        async def resolve_user_channel(
            self,
            master_session,
            organisation_id,
            user_id,
            delivery_channel,
        ):
            return OutboundDeliveryTarget(
                channel=delivery_channel,
                mapping_id=uuid4(),
                delivery_context={"service_url": "https://service", "conversation_id": "conv-123"},
            )

    monkeypatch.setattr(
        agents_handler,
        "OutboundChannelService",
        FakeOutboundChannelService,
    )

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
async def test_create_agent_resolves_delivery_through_outbound_channel_service(
    monkeypatch: pytest.MonkeyPatch,
):
    service = _FakeReminderService()
    delivery_context = {"phone_number_id": "phone-1", "recipient_wa_id": "wa-1"}

    class FakeOutboundChannelService:
        async def resolve_user_channel(
            self,
            master_session,
            organisation_id,
            user_id,
            delivery_channel,
        ):
            return OutboundDeliveryTarget(
                channel=delivery_channel,
                mapping_id=uuid4(),
                delivery_context=delivery_context,
            )

    monkeypatch.setattr(
        agents_handler,
        "OutboundChannelService",
        FakeOutboundChannelService,
    )

    body = AgentCreateRequest(
        title="Deal health check",
        agent_prompt="Summarize risks for the deal",
        deal_id=uuid4(),
        trigger_time=_future_trigger(),
        timezone="UTC",
        recurrence=ReminderRecurrence.NONE,
        delivery_channel="whatsapp",
    )

    await create_agent(
        body=body,
        response=FastAPIResponse(),
        user=type("User", (), {"organisation_id": uuid4(), "user_id": uuid4()})(),
        master_session=object(),
        service=service,
    )

    assert service.calls[0][1]["delivery_channel"] == "whatsapp"
    assert service.calls[0][1]["delivery_context"] == delivery_context


@pytest.mark.asyncio
async def test_update_agent_resolves_delivery_through_outbound_channel_service(
    monkeypatch: pytest.MonkeyPatch,
):
    service = _FakeReminderService()
    delivery_context = {"team_id": "T123", "channel_id": "C123"}

    class FakeOutboundChannelService:
        async def resolve_user_channel(
            self,
            master_session,
            organisation_id,
            user_id,
            delivery_channel,
        ):
            return OutboundDeliveryTarget(
                channel=delivery_channel,
                mapping_id=uuid4(),
                delivery_context=delivery_context,
            )

    monkeypatch.setattr(
        agents_handler,
        "OutboundChannelService",
        FakeOutboundChannelService,
    )

    await update_agent(
        reminder_id=uuid4(),
        body=agents_handler.AgentUpdateRequest(delivery_channel="slack"),
        response=FastAPIResponse(),
        user=type("User", (), {"organisation_id": uuid4(), "user_id": uuid4()})(),
        master_session=object(),
        service=service,
    )

    assert service.calls[0][0] == "update"
    assert service.calls[0][1]["updates"]["delivery_channel"] == "slack"
    assert service.calls[0][1]["updates"]["delivery_context"] == delivery_context


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
