from uuid import uuid4

from src.workers.chat_worker.services.bot_message_processing_service import (
    BotMessageProcessingService,
)

# ---------------------------------------------------------------------------
# _build_messages — system messages by conversation type
# ---------------------------------------------------------------------------


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
    assert messages[-1] == {
        "role": "user",
        "content": "create a deal named test2",
        "attachment_manifest": None,
    }


def test_teams_group_chat_system_message_instructs_fetch_tool() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="summarize",
        chat_history=None,
        bot_context={"conversation_type": "groupChat"},
    )

    assert messages[0]["role"] == "system"
    content = messages[0]["content"]
    assert "Teams group chat" in content
    assert "fetch_teams_chat_messages" in content


def test_personal_chat_has_no_system_message() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="hello",
        chat_history=None,
        bot_context={"conversation_type": "personal"},
    )

    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"


def test_build_messages_with_no_bot_context() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="hello",
        chat_history=None,
        bot_context=None,
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "user"


# ---------------------------------------------------------------------------
# _build_messages — chat history
# ---------------------------------------------------------------------------


def test_build_messages_includes_chat_history() -> None:
    service = BotMessageProcessingService(uuid4())

    history = [
        {"role": "user", "content": "previous question"},
        {"role": "assistant", "content": "previous answer"},
    ]
    messages = service._build_messages(
        message_text="follow up",
        chat_history=history,
        bot_context=None,
    )

    assert len(messages) == 3
    assert messages[0]["content"] == "previous question"
    assert messages[1]["content"] == "previous answer"
    assert messages[2]["content"] == "follow up"


# ---------------------------------------------------------------------------
# _build_messages — attachment manifest
# ---------------------------------------------------------------------------


def test_build_messages_includes_attachment_manifest() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="analyze this file",
        chat_history=None,
        bot_context={
            "current_attachments": [
                {
                    "attachment_id": "abc-123",
                    "filename": "report.pdf",
                    "mimetype": "application/pdf",
                }
            ]
        },
    )

    user_msg = messages[-1]
    assert user_msg["attachment_manifest"] is not None
    assert "[Attached files]" in user_msg["attachment_manifest"]
    assert "report.pdf" in user_msg["attachment_manifest"]


def test_build_messages_no_attachments_has_none_manifest() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="just text",
        chat_history=None,
        bot_context={},
    )

    assert messages[-1]["attachment_manifest"] is None


def test_build_messages_empty_attachments_has_none_manifest() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="just text",
        chat_history=None,
        bot_context={"current_attachments": []},
    )

    assert messages[-1]["attachment_manifest"] is None


# ---------------------------------------------------------------------------
# _merge_audio_transcript_text
# ---------------------------------------------------------------------------


def test_merge_audio_transcript_text_prefers_transcript_for_audio_only() -> None:
    service = BotMessageProcessingService(uuid4())

    merged = service._merge_audio_transcript_text("", "create a follow-up task")

    assert merged == "create a follow-up task"


def test_merge_audio_transcript_text_keeps_typed_context() -> None:
    service = BotMessageProcessingService(uuid4())

    merged = service._merge_audio_transcript_text(
        "please use this voice note",
        "create a follow-up task",
    )

    assert merged == (
        "please use this voice note\n\n[Slack audio transcript]\ncreate a follow-up task"
    )
