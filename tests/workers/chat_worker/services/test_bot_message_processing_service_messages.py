from uuid import uuid4

from src.workers.chat_worker.services.bot_message_processing_service import (
    BotMessageProcessingService,
)


def test_slack_channel_system_message_prefers_write_tools_for_crm_actions() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="create a deal named test2",
        chat_history=None,
        bot_context={"conversation_type": "channel"},
    )

    assert messages[0]["role"] == "system"
    content = messages[0]["content"]
    assert "Slack channel" in content
    assert "call the write tool immediately once required fields are present" in content
    assert "Do not ask follow-up questions for optional fields" in content
    assert messages[-1] == {"role": "user", "content": "create a deal named test2"}
