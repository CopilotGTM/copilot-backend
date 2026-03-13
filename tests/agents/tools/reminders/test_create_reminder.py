from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from src.agents.tools.reminders.create_reminder import create_reminder
from src.domain.models.organisation_tables.reminder_models import ReminderStatus, ReminderType


@dataclass
class _FakeSession:
    added: list[object] = field(default_factory=list)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: object) -> None:
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
async def test_create_reminder_returns_chat_context_error_for_slack_without_channel_context() -> (
    None
):
    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={},
    )

    result = await create_reminder(
        context=context,
        title="Test reminder",
        trigger_time="in 2 minutes",
        reminder_type="static",
        message="test",
        recurrence="none",
        timezone_name="UTC",
    )

    assert result["status"] == "error"
    assert "delivery context isn’t available" in result["message"]


@pytest.mark.asyncio
async def test_create_reminder_sends_slack_preview_and_persists_slack_delivery_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_payload: dict[str, object] = {}

    class FakeSlackBotService:
        async def send_block_kit_message(
            self,
            team_id: str,
            channel_id: str,
            blocks: list[dict],
            text_fallback: str,
        ) -> bool:
            sent_payload["team_id"] = team_id
            sent_payload["channel_id"] = channel_id
            sent_payload["blocks"] = blocks
            sent_payload["text_fallback"] = text_fallback
            return True

    monkeypatch.setattr(
        "src.service.bots.slack_bot_service.SlackBotService",
        FakeSlackBotService,
    )

    context = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={
            "team_id": "T123",
            "channel_id": "C123",
            "conversation_type": "personal",
        },
    )

    result = await create_reminder(
        context=context,
        title="Test reminder",
        trigger_time="in 2 minutes",
        reminder_type="static",
        message="test",
        recurrence="none",
        timezone_name="UTC",
    )

    assert result["status"] == "preview_sent"
    assert sent_payload["team_id"] == "T123"
    assert sent_payload["channel_id"] == "C123"
    assert sent_payload["text_fallback"] == "Reminder preview: Test reminder"

    reminder = context.session.added[0]
    assert reminder.reminder_type == ReminderType.STATIC
    assert reminder.status == ReminderStatus.PENDING_CONFIRMATION
    assert reminder.delivery_channel == "slack"
    assert reminder.delivery_context == {
        "team_id": "T123",
        "channel_id": "C123",
        "conversation_type": "personal",
    }
