"""Bot-worker contracts for prompts, attachments, status, and channel delivery."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.adapters import ChannelPayload
from src.infra.messaging.chat_stream_models import ToolUsage
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


def test_whatsapp_personal_chat_gets_mobile_plain_text_system_message() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="summarize the deal",
        chat_history=None,
        bot_context={"conversation_type": "personal"},
        provider="whatsapp",
    )

    assert messages[0]["role"] == "system"
    content = messages[0]["content"]
    assert "WhatsApp" in content
    assert "mobile" in content
    assert "plain-text" in content
    assert "concise" in content
    assert "one short clarifying question" in content
    assert messages[-1] == {
        "role": "user",
        "content": "summarize the deal",
        "attachment_manifest": None,
    }


def test_guess_mimetype_covers_whatsapp_audio_extensions() -> None:
    assert BotMessageProcessingService._guess_mimetype("voice.aac", "aac") == "audio/aac"
    assert BotMessageProcessingService._guess_mimetype("voice.amr", "amr") == "audio/amr"


def test_google_chat_personal_chat_gets_chat_plain_text_system_message() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="summarize the deal",
        chat_history=None,
        bot_context={"conversation_type": "personal"},
        provider="google_chat",
    )

    assert messages[0]["role"] == "system"
    content = messages[0]["content"]
    assert "Google Chat" in content
    assert "plain text" in content
    assert "cards or channel internals" in content
    assert messages[-1] == {
        "role": "user",
        "content": "summarize the deal",
        "attachment_manifest": None,
    }


def test_slack_personal_chat_keeps_no_system_message() -> None:
    service = BotMessageProcessingService(uuid4())

    messages = service._build_messages(
        message_text="hello",
        chat_history=None,
        bot_context={"conversation_type": "personal"},
        provider="slack",
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


def test_merge_whatsapp_audio_transcript_text_keeps_typed_context() -> None:
    service = BotMessageProcessingService(uuid4())

    merged = service._merge_whatsapp_audio_transcript_text(
        "please use this voice note",
        "create a follow-up task",
    )

    assert merged == (
        "please use this voice note\n\n[WhatsApp audio transcript]\ncreate a follow-up task"
    )


@pytest.mark.asyncio
async def test_prepare_whatsapp_audio_input_transcribes_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BotMessageProcessingService(uuid4())
    job_id = uuid4()
    persisted: dict[str, str] = {}

    async def fake_transcribe_whatsapp_audio_attachments(attachments):
        assert attachments == [
            {
                "attachment_id": "attachment-123",
                "filename": "voice.ogg",
                "mimetype": "audio/ogg",
                "source_provider": "whatsapp",
            }
        ]
        return "create a follow-up task"

    monkeypatch.setattr(
        service,
        "_transcribe_whatsapp_audio_attachments",
        fake_transcribe_whatsapp_audio_attachments,
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_persist_user_message_text",
        lambda captured_job_id, text: persisted.update(
            {"job_id": str(captured_job_id), "text": text}
        ),
    )

    message_text, bot_context = await service._prepare_bot_message_input(
        message_text="please use this voice note",
        provider="whatsapp",
        reply_context={"phone_number_id": "phone-123"},
        bot_context={
            "conversation_type": "personal",
            "current_attachments": [
                {
                    "attachment_id": "attachment-123",
                    "filename": "voice.ogg",
                    "mimetype": "audio/ogg",
                    "source_provider": "whatsapp",
                }
            ],
        },
        job_id=job_id,
    )

    assert message_text == (
        "please use this voice note\n\n[WhatsApp audio transcript]\ncreate a follow-up task"
    )
    assert bot_context["input_modality"] == "audio"
    assert bot_context["audio_origin"] == "whatsapp_attachment"
    assert persisted == {"job_id": str(job_id), "text": message_text}


@pytest.mark.asyncio
async def test_prepare_whatsapp_audio_input_keeps_typed_text_when_transcript_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BotMessageProcessingService(uuid4())

    async def fake_transcribe_whatsapp_audio_files(_files, *, phone_number_id):
        return ""

    monkeypatch.setattr(
        service,
        "_transcribe_whatsapp_audio_files",
        fake_transcribe_whatsapp_audio_files,
    )

    message_text, bot_context = await service._prepare_bot_message_input(
        message_text="typed fallback",
        provider="whatsapp",
        reply_context={"phone_number_id": "phone-123"},
        bot_context={
            "conversation_type": "personal",
            "whatsapp_audio_files": [{"media_id": "media-123"}],
        },
        job_id=uuid4(),
    )

    assert message_text == "typed fallback"
    assert "input_modality" not in bot_context


@pytest.mark.asyncio
async def test_whatsapp_audio_transcription_reads_durable_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organisation_id = uuid4()
    attachment_id = uuid4()
    attachment = SimpleNamespace(
        attachment_id=attachment_id,
        organisation_id=organisation_id,
        filename="voice.ogg",
        storage_path="attachments/voice.ogg",
    )

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: [attachment])

    class _Session:
        async def execute(self, _statement):
            return _Result()

    async def sessions(_org_id):
        yield _Session()

    class _Storage:
        paths: list[str] = []

        def __init__(self, _org_id):
            pass

        async def get_blob_content(self, storage_path: str):
            self.paths.append(storage_path)
            return b"durable audio"

        async def close(self):
            return None

    transcribe_calls: list[tuple[bytes, str]] = []

    async def transcribe(content: bytes, filename: str):
        transcribe_calls.append((content, filename))
        return "durable transcript"

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)
    monkeypatch.setattr(
        "src.infra.storage.azure_org_blob_storage.AzureOrganisationBlobStorage",
        _Storage,
    )
    monkeypatch.setattr("src.infra.media.audio_transcription.transcribe_audio", transcribe)

    transcript = await BotMessageProcessingService(
        organisation_id
    )._transcribe_whatsapp_audio_attachments(
        [
            {
                "attachment_id": str(attachment_id),
                "filename": "voice.ogg",
                "mimetype": "audio/ogg",
                "source_provider": "whatsapp",
            }
        ]
    )

    assert transcript == "durable transcript"
    assert _Storage.paths == ["attachments/voice.ogg"]
    assert transcribe_calls == [(b"durable audio", "voice.ogg")]


def test_build_status_context_supports_whatsapp() -> None:
    service = BotMessageProcessingService(uuid4())

    status_ctx = service._build_status_context(
        "whatsapp",
        {
            "phone_number_id": "phone-123",
            "recipient_wa_id": "15551234567",
            "incoming_message_id": "wamid.1",
        },
    )

    assert status_ctx == {
        "platform": "whatsapp",
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
        "incoming_message_id": "wamid.1",
    }


def test_build_status_context_returns_none_for_google_chat() -> None:
    service = BotMessageProcessingService(uuid4())

    status_ctx = service._build_status_context(
        "google_chat",
        {
            "space_name": "spaces/AAA",
            "thread_name": "spaces/AAA/threads/THREAD1",
        },
    )

    assert status_ctx is None


@pytest.mark.asyncio
async def test_update_thinking_status_sends_whatsapp_typing_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BotMessageProcessingService(uuid4())
    sent: dict[str, str] = {}

    class FakeWhatsAppBotService:
        async def send_typing_indicator(self, phone_number_id: str, message_id: str) -> bool:
            sent["phone_number_id"] = phone_number_id
            sent["message_id"] = message_id
            return True

    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.WhatsAppBotService",
        FakeWhatsAppBotService,
    )

    await service._update_thinking_status(
        {
            "platform": "whatsapp",
            "phone_number_id": "phone-123",
            "recipient_wa_id": "15551234567",
            "incoming_message_id": "wamid.1",
        },
        "Searching CRM",
    )

    assert sent == {
        "phone_number_id": "phone-123",
        "message_id": "wamid.1",
    }


@pytest.mark.asyncio
async def test_provider_sets_agent_channel_without_reply_context_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BotMessageProcessingService(uuid4())
    captured = {}

    async def fake_master_session():
        yield object()

    async def fake_get_organisation_name(_session, _organisation_id):
        return "Test Org"

    async def fake_run_agent_for_chat(**kwargs):
        captured["channel"] = kwargs["context"].channel
        captured["images"] = kwargs["images"]
        if False:
            yield None

    monkeypatch.setattr(
        "src.workers.chat_worker.services.bot_message_processing_service.acquire_master_async_session",
        fake_master_session,
    )
    monkeypatch.setattr(
        "src.workers.chat_worker.services.bot_message_processing_service.get_organisation_name",
        fake_get_organisation_name,
    )
    monkeypatch.setattr("src.agents.orchestrator.run_agent_for_chat", fake_run_agent_for_chat)

    await service._run_agent_and_accumulate(
        user_id=uuid4(),
        message_text="hello",
        provider="whatsapp",
        bot_context={
            "current_attachments": [
                {
                    "attachment_id": str(uuid4()),
                    "mimetype": "image/png",
                }
            ]
        },
        reply_context={},
    )

    assert captured["channel"] == "whatsapp"
    assert captured["images"] is None


@pytest.mark.asyncio
async def test_send_payload_dispatches_google_chat_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.adapters import ChannelPayload

    service = BotMessageProcessingService(uuid4())
    sent: dict[str, object] = {}

    class FakeGoogleChatBotService:
        async def send_message(self, **kwargs) -> bool:
            sent.update(kwargs)
            return True

    monkeypatch.setattr(
        "src.service.bots.google_chat_bot_service.GoogleChatBotService",
        FakeGoogleChatBotService,
    )

    success = await service._send_payload(
        "google_chat",
        {
            "space_name": "spaces/AAA",
            "thread_name": "spaces/AAA/threads/THREAD1",
        },
        ChannelPayload(type="message", content={"text": "Done"}),
    )

    assert success is True
    assert sent == {
        "space_name": "spaces/AAA",
        "text": "Done",
        "thread_name": "spaces/AAA/threads/THREAD1",
    }


@pytest.mark.asyncio
async def test_send_payload_sends_rendered_text_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BotMessageProcessingService(uuid4())
    sent: list[ChannelPayload] = []

    async def fake_send_payload(_provider: str, _reply_context: dict, payload: ChannelPayload):
        sent.append(payload)
        return True

    monkeypatch.setattr(service, "_send_payload", fake_send_payload)

    rendered_text = ChannelPayload(
        type="message",
        content={"text": "Created deal Acme."},
        text_fallback="Created deal Acme.",
    )

    success = await service._send_payload(
        "teams", {"service_url": "https://svc", "conversation_id": "conv"}, rendered_text
    )

    assert success is True
    assert sent == [rendered_text]


def test_channel_payload_override_uses_last_completed_tool_payload() -> None:
    payload = {
        "type": "message",
        "content": {"text": "rich card", "blocks": [{"type": "section"}]},
        "text_fallback": "rich card",
    }

    override = BotMessageProcessingService._channel_payload_override_from_frames(
        [
            ToolUsage(tool_name="create_task", status="starting"),
            ToolUsage(
                tool_name="create_task",
                status="completed",
                channel_payload=payload,
            ),
        ]
    )

    assert override == ChannelPayload(
        type="message",
        content={"text": "rich card", "blocks": [{"type": "section"}]},
        text_fallback="rich card",
        attachments=None,
        source_text=None,
    )


@pytest.mark.asyncio
async def test_send_payload_with_optional_override_preserves_rich_non_whatsapp_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BotMessageProcessingService(uuid4())
    sent: list[ChannelPayload] = []

    async def fake_send_payload(_provider: str, _reply_context: dict, payload: ChannelPayload):
        sent.append(payload)
        return True

    monkeypatch.setattr(service, "_send_payload", fake_send_payload)

    rendered_text = ChannelPayload(type="message", content={"text": "plain"})
    rich_payload = ChannelPayload(type="message", content={"text": "card"})

    success = await service._send_payload_with_optional_override(
        "teams",
        {},
        rendered_text,
        rich_payload,
    )

    assert success is True
    assert sent == [rich_payload]


@pytest.mark.asyncio
async def test_send_payload_with_optional_override_ignores_whatsapp_tool_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BotMessageProcessingService(uuid4())
    sent: list[ChannelPayload] = []

    async def fake_send_payload(_provider: str, _reply_context: dict, payload: ChannelPayload):
        sent.append(payload)
        return True

    monkeypatch.setattr(service, "_send_payload", fake_send_payload)

    rendered_text = ChannelPayload(type="message", content={"text": "llm final"})
    tool_payload = ChannelPayload(type="message", content={"text": "last tool result"})

    success = await service._send_payload_with_optional_override(
        "whatsapp",
        {},
        rendered_text,
        tool_payload,
    )

    assert success is True
    assert sent == [rendered_text]
