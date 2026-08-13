from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from pydantic import ValidationError
from src.api.routers.reminders import reminders_handler
from src.api.routers.reminders.reminders_handler import (
    _delivery_context_is_ready,
    _normalize_delivery_context,
    _provider_name,
    create_reminder,
    get_available_channels,
    get_reminder,
    get_reminder_service,
    list_reminders,
)
from src.contracts.reminders import StaticReminderCreateRequest
from src.domain.models.master_tables.user_models import UserAppRole
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderStatus,
    ReminderType,
)
from src.service.channels import OutboundChannelInfo
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.reminders.reminder_service import ReminderNotFoundError


def test_normalize_delivery_context_adds_slack_team_id_from_external_tenant() -> None:
    normalized = _normalize_delivery_context(
        delivery_channel="slack",
        conversation_context={"channel_id": "C123"},
        external_tenant_id="T123",
    )

    assert normalized == {"channel_id": "C123", "team_id": "T123"}


@pytest.mark.asyncio
async def test_reminder_dependency_binds_authenticated_crm_actor() -> None:
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        user_role=UserAppRole.MEMBER,
        user_email="member@example.com",
    )
    dependency = get_reminder_service(user=user, session=SimpleNamespace(close=AsyncMock()))
    service = await anext(dependency)
    try:
        assert getattr(service, "access_actor", None) == CRMAccessActor(
            user.user_id,
            user.user_role,
            user.user_email,
        )
    finally:
        await dependency.aclose()


def test_delivery_context_is_ready_requires_complete_teams_context() -> None:
    assert _delivery_context_is_ready(
        "teams",
        {"service_url": "https://service", "conversation_id": "conv-123"},
    )
    assert not _delivery_context_is_ready(
        "teams",
        {"service_url": "https://service", "conversation_id": ""},
    )


def test_delivery_context_is_ready_requires_complete_slack_context() -> None:
    assert _delivery_context_is_ready(
        "slack",
        {"team_id": "T123", "channel_id": "C123"},
    )
    assert not _delivery_context_is_ready(
        "slack",
        {"channel_id": "C123"},
    )


def test_delivery_context_is_ready_requires_complete_whatsapp_context() -> None:
    assert _delivery_context_is_ready(
        "whatsapp",
        {"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
    )
    assert not _delivery_context_is_ready(
        "whatsapp",
        {"phone_number_id": "phone-123"},
    )


def test_provider_name_accepts_enum_and_string() -> None:
    assert _provider_name("slack") == "slack"


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
        if kwargs.get("reminder_type") != ReminderType.STATIC:
            raise ReminderNotFoundError(reminder_id)
        return None


def test_static_reminder_create_request_rejects_agent_fields() -> None:
    with pytest.raises(ValidationError):
        StaticReminderCreateRequest.model_validate(
            {
                "title": "Follow up",
                "message_content": "Follow up with Acme",
                "agent_prompt": "Analyze this deal",
                "deal_id": str(uuid4()),
                "trigger_time": _future_trigger(),
                "timezone": "UTC",
                "recurrence": ReminderRecurrence.NONE,
                "delivery_channel": "teams",
            }
        )


def test_reminder_api_schema_does_not_export_generic_agentic_write_contracts() -> None:
    """Only static reminder write contracts remain public after legacy Agents removal."""
    from src.api.schemas import reminder_models

    assert not hasattr(reminder_models, "ReminderCreateRequest")
    assert not hasattr(reminder_models, "ReminderUpdateRequest")


@pytest.mark.asyncio
async def test_get_available_channels_returns_service_channels(monkeypatch: pytest.MonkeyPatch):
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
        reminders_handler,
        "OutboundChannelService",
        FakeOutboundChannelService,
    )

    result = await get_available_channels(
        response=FastAPIResponse(),
        user=type("User", (), {"organisation_id": uuid4(), "user_id": uuid4()})(),
        master_session=object(),
    )

    assert [channel.channel for channel in result.data.channels] == [
        "teams",
        "slack",
        "whatsapp",
    ]
    assert [channel.available for channel in result.data.channels] == [True, False, True]
    assert [channel.icon_url for channel in result.data.channels] == [
        "/integrations/microsoft-teams.png",
        "/integrations/slack.png",
        "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg",
    ]


@pytest.mark.asyncio
async def test_create_reminder_always_creates_static_reminder(monkeypatch: pytest.MonkeyPatch):
    service = _FakeReminderService()

    async def fake_resolve_delivery_context(*args, **kwargs):
        return {"service_url": "https://service", "conversation_id": "conv-123"}

    monkeypatch.setattr(
        reminders_handler,
        "_resolve_delivery_context",
        fake_resolve_delivery_context,
    )

    body = StaticReminderCreateRequest(
        title="Follow up",
        message_content="Follow up with Acme",
        trigger_time=_future_trigger(),
        timezone="UTC",
        recurrence=ReminderRecurrence.NONE,
        delivery_channel="teams",
    )

    await create_reminder(
        body=body,
        response=FastAPIResponse(),
        user=type("User", (), {"organisation_id": uuid4(), "user_id": uuid4()})(),
        master_session=object(),
        service=service,
    )

    assert service.calls[0][1]["reminder_type"] == ReminderType.STATIC
    assert service.calls[0][1]["agent_prompt"] is None
    assert service.calls[0][1]["deal_id"] is None


@pytest.mark.asyncio
async def test_list_reminders_applies_static_type_filter() -> None:
    service = _FakeReminderService()

    await list_reminders(
        response=FastAPIResponse(),
        service=service,
        query=reminders_handler.ReminderListQueryParams(status=ReminderStatus.ACTIVE.value),
    )

    assert service.calls[0][1]["reminder_type"] == ReminderType.STATIC


@pytest.mark.asyncio
async def test_get_reminder_uses_static_type_boundary() -> None:
    service = _FakeReminderService()
    reminder_id = uuid4()

    await get_reminder(
        reminder_id=reminder_id,
        response=FastAPIResponse(),
        service=service,
    )

    assert service.calls[0] == (
        "get",
        {"reminder_id": reminder_id, "reminder_type": ReminderType.STATIC},
    )
