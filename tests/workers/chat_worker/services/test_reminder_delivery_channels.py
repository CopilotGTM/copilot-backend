from uuid import uuid4

import pytest
from src.workers.chat_worker.services.reminder_execution_service import ReminderExecutionService


@pytest.mark.asyncio
async def test_send_text_uses_slack_bot_service(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class FakeSlackBotService:
        async def _send_message(self, team_id: str, channel_id: str, text: str) -> bool:
            captured["team_id"] = team_id
            captured["channel_id"] = channel_id
            captured["text"] = text
            return True

    monkeypatch.setattr("src.service.bots.slack_bot_service.SlackBotService", FakeSlackBotService)

    service = ReminderExecutionService(uuid4())
    sent = await service._send_text(
        delivery_channel="slack",
        delivery_context={"team_id": "T123", "channel_id": "C123"},
        text="hello",
    )

    assert sent is True
    assert captured == {"team_id": "T123", "channel_id": "C123", "text": "hello"}


@pytest.mark.asyncio
async def test_send_payload_uses_slack_block_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeSlackBotService:
        async def send_block_kit_message(
            self,
            team_id: str,
            channel_id: str,
            blocks: list[dict[str, str]],
            text_fallback: str,
        ) -> bool:
            captured["team_id"] = team_id
            captured["channel_id"] = channel_id
            captured["blocks"] = blocks
            captured["text_fallback"] = text_fallback
            return True

    class FakePayload:
        def __init__(self) -> None:
            self.content = {
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
            }
            self.text_fallback = "hi"

    monkeypatch.setattr("src.service.bots.slack_bot_service.SlackBotService", FakeSlackBotService)

    service = ReminderExecutionService(uuid4())
    sent = await service._send_payload(
        delivery_channel="slack",
        delivery_context={"team_id": "T123", "channel_id": "C123"},
        payload=FakePayload(),
        title="Alert",
        response_text="hi",
    )

    assert sent is True
    assert captured["team_id"] == "T123"
    assert captured["channel_id"] == "C123"
    assert captured["text_fallback"] == "hi"
