from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.infra.config.configs import slack_config, teams_config, whatsapp_config
from src.service.bots.whatsapp_bot_service import (
    WhatsAppBotService,
    WhatsAppInboundMedia,
    WhatsAppInboundMessage,
)


def _message() -> WhatsAppInboundMessage:
    return WhatsAppInboundMessage(
        message_id="wamid.1",
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="hello from whatsapp",
        profile_name="Nikhil",
    )


@pytest.mark.asyncio
async def test_unlinked_user_gets_login_message(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WhatsAppBotService()
    send_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(
        service,
        "_get_login_message_with_state",
        lambda message: f"login for {message.recipient_wa_id}",
    )
    monkeypatch.setattr(service, "send_text_message", send_mock)

    await service._route_message(_message())

    send_mock.assert_awaited_once_with(
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="login for 15551234567",
    )


def test_login_message_uses_whatsapp_auth_login_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def setex(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(
        "src.service.bots.auth_state_service.get_redis_client",
        lambda: FakeRedis(),
    )
    monkeypatch.setattr(whatsapp_config, "WHATSAPP_AUTH_LOGIN_URL", "https://wa.example/bots/auth")
    monkeypatch.setattr(slack_config, "SLACK_AUTH_LOGIN_URL", "https://slack.example/bots/auth")
    monkeypatch.setattr(teams_config, "TEAMS_AUTH_LOGIN_URL", "https://teams.example/bots/auth")

    login_message = WhatsAppBotService()._get_login_message_with_state(_message())

    assert "https://wa.example/bots/auth?" in login_message
    assert "provider=whatsapp" in login_message
    assert "slack.example" not in login_message
    assert "teams.example" not in login_message


def test_login_message_does_not_fallback_to_slack_or_teams_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whatsapp_config, "WHATSAPP_AUTH_LOGIN_URL", "")
    monkeypatch.setattr(slack_config, "SLACK_AUTH_LOGIN_URL", "https://slack.example/bots/auth")
    monkeypatch.setattr(teams_config, "TEAMS_AUTH_LOGIN_URL", "https://teams.example/bots/auth")

    login_message = WhatsAppBotService()._get_login_message_with_state(_message())

    assert "Unable to generate a login link right now" in login_message
    assert "slack.example" not in login_message
    assert "teams.example" not in login_message


@pytest.mark.asyncio
async def test_linked_user_enqueues_chat_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
        timezone="Asia/Kolkata",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))

    async def fake_send_chat_task_async(name, args, kwargs, queue):
        captured["name"] = name
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["queue"] = queue
        return "task-1"

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(_message())

    assert captured["name"] == "chat.process_bot_message"
    assert captured["args"][3] == "whatsapp"
    assert captured["args"][4] == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
        "incoming_message_id": "wamid.1",
    }
    assert captured["kwargs"]["bot_context"]["conversation_type"] == "personal"
    assert captured["kwargs"]["bot_context"]["external_user_id"] == "15551234567"
    assert captured["kwargs"]["bot_context"]["user_timezone"] == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_linked_user_sends_typing_indicator_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    captured: dict[str, object] = {}
    typing_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(service, "send_typing_indicator", typing_mock)

    async def fake_send_chat_task_async(name, args, kwargs, queue):
        captured["reply_context"] = args[4]
        return "task-1"

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(_message())

    typing_mock.assert_awaited_once_with(
        phone_number_id="phone-123",
        message_id="wamid.1",
    )
    assert captured["reply_context"] == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
        "incoming_message_id": "wamid.1",
    }


@pytest.mark.asyncio
async def test_linked_user_enqueues_when_typing_indicator_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(
        service,
        "send_typing_indicator",
        AsyncMock(side_effect=RuntimeError("send failed")),
    )

    async def fake_send_chat_task_async(name, args, kwargs, queue):
        captured["reply_context"] = args[4]
        return "task-1"

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(_message())

    assert captured["reply_context"] == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
        "incoming_message_id": "wamid.1",
    }


@pytest.mark.asyncio
async def test_linked_user_gets_temporary_failure_message_when_enqueue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    send_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "send_text_message", send_mock)

    async def fake_send_chat_task_async(*_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(_message())

    send_mock.assert_awaited_once_with(
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text=(
            "I'm sorry, I'm having trouble processing your request right now. "
            "Please try again in a moment."
        ),
    )


@pytest.mark.asyncio
async def test_linked_user_audio_message_enqueues_chat_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    message = WhatsAppInboundMessage(
        message_id="wamid.audio",
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="",
        profile_name="Nikhil",
        audio_files=[
            {
                "media_id": "media-123",
                "mimetype": "audio/ogg",
                "filename": "whatsapp-audio.ogg",
                "message_id": "wamid.audio",
            }
        ],
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))

    async def fake_send_chat_task_async(name, args, kwargs, queue):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "task-1"

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(message)

    assert captured["args"][2] == ""
    bot_context = captured["kwargs"]["bot_context"]
    assert bot_context["whatsapp_audio_files"] == [
        {
            "media_id": "media-123",
            "mimetype": "audio/ogg",
            "filename": "whatsapp-audio.ogg",
            "message_id": "wamid.audio",
        }
    ]


def test_extract_inbound_audio_message() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "contacts": [
                                {
                                    "wa_id": "1555",
                                    "profile": {"name": "Nikhil"},
                                }
                            ],
                            "messages": [
                                {
                                    "id": "wamid.audio",
                                    "from": "1555",
                                    "type": "audio",
                                    "audio": {
                                        "id": "media-123",
                                        "mime_type": "audio/ogg; codecs=opus",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_text_messages(payload)

    assert len(messages) == 1
    assert messages[0].text == ""
    assert messages[0].profile_name == "Nikhil"
    assert messages[0].audio_files == [
        {
            "media_id": "media-123",
            "mimetype": "audio/ogg; codecs=opus",
            "filename": "whatsapp-audio.ogg",
            "message_id": "wamid.audio",
        }
    ]


def test_extract_inbound_document_message() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "messages": [
                                {
                                    "id": "wamid.doc",
                                    "from": "1555",
                                    "type": "document",
                                    "document": {
                                        "id": "media-doc-1",
                                        "filename": "proposal.pdf",
                                        "mime_type": "application/pdf",
                                        "caption": "Please summarize this",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_messages(payload)

    assert len(messages) == 1
    assert messages[0].text == "Please summarize this"
    assert messages[0].media_files == [
        WhatsAppInboundMedia(
            media_id="media-doc-1",
            media_type="document",
            mimetype="application/pdf",
            filename="proposal.pdf",
            caption="Please summarize this",
            message_id="wamid.doc",
        )
    ]


def test_extract_inbound_image_message() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "messages": [
                                {
                                    "id": "wamid.image",
                                    "from": "1555",
                                    "type": "image",
                                    "image": {
                                        "id": "media-image-1",
                                        "mime_type": "image/jpeg",
                                        "caption": "What is in this image?",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_messages(payload)

    assert len(messages) == 1
    assert messages[0].text == "What is in this image?"
    assert messages[0].media_files == [
        WhatsAppInboundMedia(
            media_id="media-image-1",
            media_type="image",
            mimetype="image/jpeg",
            filename="whatsapp-image.jpg",
            caption="What is in this image?",
            message_id="wamid.image",
        )
    ]


def test_extract_inbound_contact_card_as_text_context() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "messages": [
                                {
                                    "id": "wamid.contact",
                                    "from": "1555",
                                    "type": "contacts",
                                    "contacts": [
                                        {
                                            "name": {
                                                "formatted_name": "Ada Lovelace",
                                                "first_name": "Ada",
                                                "last_name": "Lovelace",
                                            },
                                            "org": {"company": "Analytical Engines"},
                                            "phones": [{"phone": "+15550001", "type": "WORK"}],
                                            "emails": [
                                                {"email": "ada@example.com", "type": "WORK"}
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_messages(payload)

    assert len(messages) == 1
    assert "[Shared WhatsApp contact]" in messages[0].text
    assert "Ada Lovelace" in messages[0].text
    assert "Analytical Engines" in messages[0].text
    assert "+15550001" in messages[0].text
    assert "ada@example.com" in messages[0].text


def test_extract_inbound_location_as_text_context() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "messages": [
                                {
                                    "id": "wamid.location",
                                    "from": "1555",
                                    "type": "location",
                                    "location": {
                                        "latitude": 12.9716,
                                        "longitude": 77.5946,
                                        "name": "Bengaluru Office",
                                        "address": "MG Road",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_messages(payload)

    assert len(messages) == 1
    assert "[Shared WhatsApp location]" in messages[0].text
    assert "12.9716" in messages[0].text
    assert "77.5946" in messages[0].text
    assert "Bengaluru Office" in messages[0].text
    assert "MG Road" in messages[0].text


def test_extract_inbound_unsupported_message_has_reason() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "messages": [
                                {
                                    "id": "wamid.sticker",
                                    "from": "1555",
                                    "type": "sticker",
                                    "sticker": {"id": "sticker-1"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_messages(payload)

    assert len(messages) == 1
    assert messages[0].text == ""
    assert messages[0].unsupported_reason == "sticker"


@pytest.mark.asyncio
async def test_linked_user_document_message_enqueues_with_attachment_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    message = WhatsAppInboundMessage(
        message_id="wamid.doc",
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="Please summarize",
        media_files=[
            WhatsAppInboundMedia(
                media_id="media-doc-1",
                media_type="document",
                mimetype="application/pdf",
                filename="proposal.pdf",
                caption="Please summarize",
                message_id="wamid.doc",
            )
        ],
    )
    attachment_refs = [
        {
            "attachment_id": str(uuid4()),
            "filename": "proposal.pdf",
            "mimetype": "application/pdf",
            "source_provider": "whatsapp",
        }
    ]
    captured: dict[str, object] = {}

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_bot_job", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))
    monkeypatch.setattr(
        service,
        "_ingest_media_files",
        AsyncMock(return_value=attachment_refs),
    )

    async def fake_send_chat_task_async(name, args, kwargs, queue):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "task-1"

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(message)

    assert captured["args"][2] == "Please summarize"
    assert captured["kwargs"]["bot_context"]["current_attachments"] == attachment_refs
    service._create_bot_job.assert_awaited_once_with(
        user=user,
        message_text="Please summarize",
        attachment_refs=attachment_refs,
    )


@pytest.mark.asyncio
async def test_unsupported_only_message_sends_safe_reply_without_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    message = WhatsAppInboundMessage(
        message_id="wamid.sticker",
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="",
        unsupported_reason="sticker",
    )
    send_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "send_text_message", send_mock)

    async def fake_send_chat_task_async(*_args, **_kwargs):
        raise AssertionError("unsupported-only messages should not enqueue agent work")

    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        fake_send_chat_task_async,
    )

    await service._route_message(message)

    send_mock.assert_awaited_once()
    assert "sticker" in send_mock.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_status_only_payload_is_ignored() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"statuses": [{"id": "status-1"}]}}]}],
    }

    messages = service._extract_inbound_text_messages(payload)

    assert messages == []


@pytest.mark.asyncio
async def test_video_message_is_unsupported() -> None:
    service = WhatsAppBotService()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-123"},
                            "messages": [{"id": "wamid.1", "from": "1555", "type": "video"}],
                        }
                    }
                ]
            }
        ],
    }

    messages = service._extract_inbound_messages(payload)

    assert len(messages) == 1
    assert messages[0].unsupported_reason == "video"


class _FakeAsyncSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def flush(self):
        pass

    async def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))


@pytest.mark.asyncio
async def test_create_bot_job_creates_whatsapp_personal_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    captured_jobs = []

    class TrackingSession(_FakeAsyncSession):
        def add(self, obj):
            super().add(obj)
            if hasattr(obj, "job_type"):
                captured_jobs.append(obj)

    async def gen(_org_id):
        yield TrackingSession()

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", gen)

    job_id = await service._create_bot_job(user=user, message_text="hello")

    assert isinstance(job_id, UUID)
    assert len(captured_jobs) == 1
    assert captured_jobs[0].job_type == "whatsapp_chat_response"
    assert captured_jobs[0].chat_type == "WHATSAPP_CHAT"
