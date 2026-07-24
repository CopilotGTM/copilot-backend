"""Regression coverage for deterministic calendar-review channel delivery."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.adapters import ChannelPayload
from src.infra.messaging.chat_stream_models import ToolUsage
from src.workers.chat_worker.services.bot_message_processing_service import (
    BotMessageProcessingService,
)


def test_calendar_review_override_preserves_force_flag() -> None:
    """Tool frames must carry exact-review intent across the bot delivery boundary."""
    override = BotMessageProcessingService._channel_payload_override_from_frames(
        [
            ToolUsage(
                tool_name="SendCalendarInvite",
                status="completed",
                channel_payload={
                    "type": "message",
                    "content": {"text": "Meeting\n25 July, 3:30–4:00 PM IST"},
                    "force_override": True,
                },
            )
        ]
    )

    assert override is not None
    assert override.force_override is True


@pytest.mark.asyncio
async def test_calendar_review_override_replaces_whatsapp_model_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact action review should replace model prose even on text-only WhatsApp."""
    service = BotMessageProcessingService(uuid4())
    sent: list[ChannelPayload] = []

    async def fake_send_payload(
        _provider: str,
        _reply_context: dict,
        payload: ChannelPayload,
    ) -> bool:
        """Capture the payload selected by the channel override boundary."""
        sent.append(payload)
        return True

    monkeypatch.setattr(service, "_send_payload", fake_send_payload)

    rendered_text = ChannelPayload(type="message", content={"text": "llm paraphrase"})
    canonical_review = ChannelPayload(
        type="message",
        content={"text": "Meeting\n25 July, 3:30–4:00 PM IST"},
        force_override=True,
    )

    success = await service._send_payload_with_optional_override(
        "whatsapp",
        {},
        rendered_text,
        canonical_review,
    )

    assert success is True
    assert sent == [canonical_review]
