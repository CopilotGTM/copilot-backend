from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from src.agents.tools.reminders.create_reminder import (
    _resolve_current_reminder_delivery_target,
    _resolve_reminder_delivery_target,
    create_reminder,
)
from src.domain.models.organisation_tables.reminder_models import (
    ReminderStatus,
    ReminderType,
)


@dataclass
class _FakeSession:
    added: list[object] = field(default_factory=list)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: object) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class _FakeContext:
    organisation_id: object
    user_id: object
    channel: str
    metadata: dict
    session: _FakeSession = field(default_factory=_FakeSession)

    @asynccontextmanager
    async def acquire_session(self):
        yield self.session


@pytest.mark.asyncio
async def test_create_reminder_rejects_generic_title_without_creating_row() -> None:
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T123", "channel_id": "C123"},
    )

    result = await create_reminder(
        context=context,
        title="set a reminder",
        trigger_time=_future_iso(),
        reminder_type="static",
        message="Follow up with the customer",
        recurrence="none",
        timezone_name="UTC",
    )

    assert result["status"] == "error"
    assert result["missing_fields"] == ["title"]
    assert context.session.added == []


@pytest.mark.asyncio
async def test_create_reminder_requires_static_message() -> None:
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T123", "channel_id": "C123"},
    )

    result = await create_reminder(
        context=context,
        title="Follow up with Acme",
        trigger_time=_future_iso(),
        reminder_type="static",
        message=None,
        recurrence="none",
        timezone_name="UTC",
    )

    assert result["status"] == "error"
    assert result["missing_fields"] == ["message"]


@pytest.mark.asyncio
async def test_create_reminder_returns_setup_error_without_delivery_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acquire_master_async_session() -> AsyncGenerator[_EmptyMasterSession, None]:
        yield _EmptyMasterSession()

    monkeypatch.setattr(
        "src.agents.tools.reminders.create_reminder.acquire_master_async_session",
        fake_acquire_master_async_session,
    )

    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="web",
        metadata={},
    )

    result = await create_reminder(
        context=context,
        title="Follow up with Acme",
        trigger_time=_future_iso(),
        reminder_type="static",
        message="Follow up with Acme",
        recurrence="none",
        timezone_name="UTC",
    )

    assert result["status"] == "error"
    assert "connect Teams, Slack, or WhatsApp" in result["message"]


@pytest.mark.asyncio
async def test_create_reminder_creates_active_slack_reminder_and_success_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: dict[str, object] = {}
    monkeypatch.setattr(
        "src.service.reminders.reminder_service.schedule_reminder",
        lambda org_id, reminder_id, trigger_epoch: scheduled.update(
            {"org_id": org_id, "reminder_id": reminder_id, "trigger_epoch": trigger_epoch}
        ),
    )

    org_id = uuid4()
    user_id = uuid4()
    context = _FakeContext(
        organisation_id=org_id,
        user_id=user_id,
        channel="slack",
        metadata={
            "team_id": "T123",
            "channel_id": "C123",
            "conversation_type": "personal",
        },
    )

    result = await create_reminder(
        context=context,
        title="Follow up with Acme",
        trigger_time=_future_iso(),
        reminder_type="static",
        message="Follow up with Acme",
        recurrence="none",
        timezone_name="UTC",
    )

    assert result["status"] == "created"
    assert result["title"] == "Follow up with Acme"
    assert result["delivery_channel"] == "slack"
    assert result["_bot_channel_payload"]["content"]["blocks"][0]["type"] == "header"

    reminder = context.session.added[0]
    assert reminder.reminder_type == ReminderType.STATIC
    assert reminder.status == ReminderStatus.ACTIVE
    assert reminder.organisation_id == org_id
    assert reminder.user_id == user_id
    assert reminder.delivery_channel == "slack"
    assert reminder.delivery_context == {
        "team_id": "T123",
        "channel_id": "C123",
        "conversation_type": "personal",
    }
    assert scheduled["reminder_id"] == reminder.reminder_id


@pytest.mark.asyncio
async def test_create_reminder_creates_active_agentic_whatsapp_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.service.reminders.reminder_service.schedule_reminder",
        lambda *_args, **_kwargs: None,
    )
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="whatsapp",
        metadata={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
    )

    result = await create_reminder(
        context=context,
        title="Acme health check",
        trigger_time=_future_iso(),
        reminder_type="agentic",
        agent_prompt="Review the Acme deal and identify blockers.",
        recurrence="none",
        timezone_name="UTC",
    )

    assert result["status"] == "created"
    assert result["delivery_channel"] == "whatsapp"
    assert result["_bot_channel_payload"]["content"]["text"].startswith("Reminder created")

    reminder = context.session.added[0]
    assert reminder.reminder_type == ReminderType.AGENTIC
    assert reminder.status == ReminderStatus.ACTIVE
    assert reminder.agent_config == {
        "agent_type": "general",
        "prompt": "Review the Acme deal and identify blockers.",
    }
    assert reminder.delivery_context == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
    }


@pytest.mark.asyncio
async def test_web_reminder_falls_back_to_connected_teams_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    rows_by_provider = {
        "teams": (
            {
                "service_url": "https://teams.example",
                "conversation_id": "conv-123",
                "conversation_type": "personal",
            },
            None,
        )
    }

    async def fake_acquire_master_async_session() -> AsyncGenerator[_MappingMasterSession, None]:
        yield _MappingMasterSession(rows_by_provider)

    monkeypatch.setattr(
        "src.agents.tools.reminders.create_reminder.acquire_master_async_session",
        fake_acquire_master_async_session,
    )

    context = _FakeContext(
        organisation_id=org_id,
        user_id=user_id,
        channel="web",
        metadata={},
    )

    target = await _resolve_reminder_delivery_target(context)

    assert target == (
        "teams",
        {
            "service_url": "https://teams.example",
            "conversation_id": "conv-123",
            "conversation_type": "personal",
        },
    )


def test_current_channel_whatsapp_delivery_context_wins() -> None:
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="whatsapp",
        metadata={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
    )

    target = _resolve_current_reminder_delivery_target(context)

    assert target == (
        "whatsapp",
        {"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
    )


def _future_iso() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


class _EmptyResult:
    def first(self):
        return None


class _EmptyMasterSession:
    async def execute(self, _stmt):
        return _EmptyResult()


class _MappingResult:
    def __init__(self, row: tuple[dict, str | None] | None) -> None:
        self._row = row

    def first(self):
        return self._row


class _MappingMasterSession:
    def __init__(self, rows_by_provider: dict[str, tuple[dict, str | None]]) -> None:
        self.rows = list(rows_by_provider.values())

    async def execute(self, _stmt):
        if not self.rows:
            return _MappingResult(None)
        return _MappingResult(self.rows.pop(0))
