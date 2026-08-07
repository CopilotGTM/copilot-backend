"""WhatsApp intake, terminal enqueue, media, and provider-send contracts."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import JobStatus
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


def _patch_durable_job(monkeypatch, service: WhatsAppBotService):
    job = SimpleNamespace(job_id=uuid4(), job_metadata={"enqueue_status": "pending"})
    create_mock = AsyncMock(return_value=job)
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_create_or_get_durable_job", create_mock)
    monkeypatch.setattr(service, "_enqueue_durable_job", enqueue_mock)
    return job, create_mock, enqueue_mock


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
    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))
    job, create_mock, enqueue_mock = _patch_durable_job(monkeypatch, service)

    await service._route_message(_message())

    envelope = create_mock.await_args.kwargs["dispatch_envelope"]
    assert envelope["provider"] == "whatsapp"
    assert envelope["reply_context"] == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
        "incoming_message_id": "wamid.1",
    }
    assert envelope["bot_context"]["conversation_type"] == "personal"
    assert envelope["bot_context"]["external_user_id"] == "15551234567"
    assert envelope["bot_context"]["user_timezone"] == "Asia/Kolkata"
    enqueue_mock.assert_awaited_once_with(user.organisation_id, job.job_id)


@pytest.mark.asyncio
async def test_linked_user_does_not_enqueue_job_waiting_for_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rapid same-session messages wait durably instead of running in parallel."""
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    job = SimpleNamespace(
        job_id=uuid4(),
        job_metadata={"enqueue_status": "waiting_for_predecessor"},
    )
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_or_get_durable_job", AsyncMock(return_value=job))
    monkeypatch.setattr(service, "_enqueue_durable_job", enqueue_mock)

    await service._route_message(_message())

    # The predecessor's terminal path is the only place allowed to release this job.
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_linked_user_records_whatsapp_customer_service_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(user_id=uuid4(), organisation_id=uuid4())
    mapping = SimpleNamespace(
        conversation_context={"phone_number_id": "phone-123", "recipient_wa_id": "15551234567"},
        last_used_at=None,
    )
    commits = 0

    class _Session:
        async def commit(self):
            nonlocal commits
            commits += 1

    async def _sessions():
        yield _Session()

    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.acquire_master_async_session",
        _sessions,
    )
    monkeypatch.setattr(
        service._identity_service,
        "get_user_for_external_identity",
        AsyncMock(return_value=(user, mapping)),
    )

    resolved_user, context = await service._get_linked_user(
        "15551234567",
        "phone-123",
        inbound_message_id="wamid.1",
    )

    assert resolved_user is user
    assert context["phone_number_id"] == "phone-123"
    assert context["recipient_wa_id"] == "15551234567"
    assert context["last_inbound_message_id"] == "wamid.1"
    assert context["last_inbound_at"].endswith("+00:00")
    assert mapping.conversation_context == context
    assert mapping.last_used_at is not None
    assert commits == 1


@pytest.mark.asyncio
async def test_get_linked_user_raises_durability_error_on_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.service.bots import whatsapp_bot_service

    service = WhatsAppBotService()

    class _Session:
        pass

    async def _sessions():
        yield _Session()

    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.acquire_master_async_session",
        _sessions,
    )
    monkeypatch.setattr(
        service._identity_service,
        "get_user_for_external_identity",
        AsyncMock(side_effect=RuntimeError("master database unavailable")),
    )

    with pytest.raises(whatsapp_bot_service.WhatsAppInboundDurabilityError):
        await service._get_linked_user("15551234567", "phone-123")


@pytest.mark.asyncio
async def test_linked_user_defers_typing_indicator_until_worker_owns_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued follow-up must not show typing before it becomes the head job."""
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    calls: list[str] = []

    async def create_durable_job(**_kwargs):
        calls.append("persist")
        return SimpleNamespace(job_id=uuid4(), job_metadata={"enqueue_status": "pending"})

    async def enqueue_durable_job(*_args):
        calls.append("enqueue")
        return True

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "_create_or_get_durable_job", create_durable_job)
    monkeypatch.setattr(service, "_enqueue_durable_job", enqueue_durable_job)

    await service._route_message(_message())

    # Waiting messages must not show typing while an earlier session job is active.
    assert calls == ["persist", "enqueue"]


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
    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(
        service,
        "send_typing_indicator",
        AsyncMock(side_effect=RuntimeError("send failed")),
    )
    _, create_mock, _ = _patch_durable_job(monkeypatch, service)

    await service._route_message(_message())

    assert create_mock.await_args.kwargs["dispatch_envelope"]["reply_context"] == {
        "phone_number_id": "phone-123",
        "recipient_wa_id": "15551234567",
        "incoming_message_id": "wamid.1",
    }


@pytest.mark.asyncio
async def test_linked_user_reports_terminal_enqueue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    send_mock = AsyncMock(return_value=True)
    advance_mock = AsyncMock(return_value=False)
    chat_session_id = uuid4()

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "send_text_message", send_mock)
    monkeypatch.setattr(service, "enqueue_next_session_job", advance_mock)
    job, _, enqueue_mock = _patch_durable_job(monkeypatch, service)
    job.job_metadata["session_id"] = str(chat_session_id)
    enqueue_mock.return_value = False

    await service._route_message(_message())

    send_mock.assert_awaited_once_with(
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="Sorry, I couldn't start processing that message. Please try again.",
    )
    advance_mock.assert_awaited_once_with(user.organisation_id, chat_session_id)


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
    attachment_refs = [
        {
            "attachment_id": str(uuid4()),
            "filename": "whatsapp-audio.ogg",
            "mimetype": "audio/ogg",
            "source_provider": "whatsapp",
        }
    ]

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_ingest_media_files", AsyncMock(return_value=attachment_refs))
    _, create_mock, _ = _patch_durable_job(monkeypatch, service)

    await service._route_message(message)

    bot_context = create_mock.await_args.kwargs["dispatch_envelope"]["bot_context"]
    assert bot_context["current_attachments"] == attachment_refs
    assert "whatsapp_audio_files" not in bot_context


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
    assert messages[0].audio_files is None
    assert messages[0].media_files == [
        WhatsAppInboundMedia(
            media_id="media-123",
            media_type="audio",
            mimetype="audio/ogg; codecs=opus",
            filename="whatsapp-audio.ogg",
            message_id="wamid.audio",
        )
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


def test_extract_inbound_forward_and_reply_context() -> None:
    """Meta forwarding and reply fields survive normalization for later safety checks."""
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
                                    "id": "wamid.forwarded",
                                    "from": "15551234567",
                                    "type": "text",
                                    "text": {"body": "Add this to the previous deal"},
                                    "context": {
                                        "forwarded": True,
                                        "frequently_forwarded": True,
                                        "id": "wamid.replied-to",
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

    # Meta's flags are durable safety inputs, not presentation-only metadata.
    assert messages[0].is_forwarded is True
    assert messages[0].is_frequently_forwarded is True
    assert messages[0].reply_to_message_id == "wamid.replied-to"


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
    assert (
        "Google Maps: https://www.google.com/maps/search/?api=1&query=12.9716%2C77.5946"
    ) in messages[0].text


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
    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=False))
    monkeypatch.setattr(
        service,
        "_ingest_media_files",
        AsyncMock(return_value=attachment_refs),
    )
    _, create_mock, _ = _patch_durable_job(monkeypatch, service)

    await service._route_message(message)

    create_kwargs = create_mock.await_args.kwargs
    assert create_kwargs["dispatch_envelope"]["message_text"] == "Please summarize"
    assert create_kwargs["dispatch_envelope"]["bot_context"]["current_attachments"] == (
        attachment_refs
    )
    create_mock.assert_awaited_once_with(
        user=user,
        message=message,
        attachment_refs=attachment_refs,
        dispatch_envelope=create_kwargs["dispatch_envelope"],
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
async def test_video_message_is_reported_as_unsupported() -> None:
    """Video remains outside the supported WhatsApp modalities until explicitly designed."""
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
                                    "id": "wamid.1",
                                    "from": "1555",
                                    "type": "video",
                                    "video": {
                                        "id": "media-video-1",
                                        "mime_type": "video/mp4",
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
    assert messages[0].message_type == "video"
    assert messages[0].media_files is None
    assert messages[0].unsupported_reason == "video"


def test_whatsapp_idempotency_key_includes_business_number_sender_and_wamid() -> None:
    assert WhatsAppBotService._idempotency_key(_message()) == (
        "whatsapp:phone-123:15551234567:wamid.1"
    )


def test_media_caption_requires_explicit_write_language_for_action_authority() -> None:
    """A descriptive caption supplies content but cannot silently inherit an old write target."""
    descriptive = WhatsAppInboundMessage(
        message_id="wamid.caption",
        phone_number_id="phone-123",
        recipient_wa_id="1555",
        text="Delta storefront",
        message_type="image",
    )
    explicit = WhatsAppInboundMessage(
        message_id="wamid.command",
        phone_number_id="phone-123",
        recipient_wa_id="1555",
        text="Add this image as a note to Delta",
        message_type="image",
    )

    assert WhatsAppBotService._has_direct_instruction_text(descriptive) is False
    assert WhatsAppBotService._has_direct_instruction_text(explicit) is True


@pytest.mark.asyncio
async def test_job_persistence_failure_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.service.bots import whatsapp_bot_service

    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
        timezone=None,
    )
    dispatch_mock = AsyncMock()

    monkeypatch.setattr(service, "_get_linked_user", AsyncMock(return_value=(user, {})))
    monkeypatch.setattr(service, "send_typing_indicator", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service,
        "_create_or_get_durable_job",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
        raising=False,
    )
    monkeypatch.setattr(
        "src.workers.chat_worker.client.send_chat_task_async",
        dispatch_mock,
    )

    with pytest.raises(whatsapp_bot_service.WhatsAppInboundDurabilityError):
        await service._route_message(_message())

    dispatch_mock.assert_not_awaited()


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return SimpleNamespace(first=lambda: self.value)


class _DurableJobSession:
    def __init__(self, results, *, commit_error=None):
        self.results = list(results)
        self.commit_error = commit_error
        self.added = []
        self.executed = []
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, statement):
        """Record each query so tests can assert the durable lookup contract."""
        self.executed.append(statement)
        return _ScalarResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commit_count += 1
        if self.commit_error:
            raise self.commit_error

    async def rollback(self):
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_durable_job_commits_message_links_and_job_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist provider retry identity in the column and compatibility metadata."""
    from src.domain.models.organisation_tables.chat_message_models import (
        ChatMessage,
        ChatMessageAttachmentLink,
    )
    from src.domain.models.organisation_tables.chat_session_models import ChatSession
    from src.domain.models.organisation_tables.job_models import ChatMessageJob

    service = WhatsAppBotService()
    user = SimpleNamespace(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="test@example.com",
    )
    attachment_id = uuid4()
    session = _DurableJobSession([None, None, None])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)

    job = await service._create_or_get_durable_job(
        user=user,
        message=_message(),
        attachment_refs=[
            {
                "attachment_id": str(attachment_id),
                "filename": "proposal.pdf",
                "mimetype": "application/pdf",
                "source_provider": "whatsapp",
            }
        ],
        dispatch_envelope={"organisation_id": str(user.organisation_id)},
    )

    assert isinstance(job, ChatMessageJob)
    assert session.commit_count == 1
    chat_session = next(item for item in session.added if isinstance(item, ChatSession))
    inbound_message = next(item for item in session.added if isinstance(item, ChatMessage))
    assert chat_session.updated_at == inbound_message.created_at
    assert sum(isinstance(item, ChatMessage) for item in session.added) == 1
    assert sum(isinstance(item, ChatMessageAttachmentLink) for item in session.added) == 1
    assert sum(isinstance(item, ChatMessageJob) for item in session.added) == 1
    assert job.idempotency_key == "whatsapp:phone-123:15551234567:wamid.1"
    assert job.job_metadata["idempotency_key"] == ("whatsapp:phone-123:15551234567:wamid.1")
    assert job.job_metadata["dispatch_envelope"] == {"organisation_id": str(user.organisation_id)}


@pytest.mark.asyncio
async def test_durable_job_returns_existing_job_for_duplicate_wamid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query the normalized key column when converging a duplicate webhook."""
    from src.domain.models.organisation_tables.job_models import ChatMessageJob

    service = WhatsAppBotService()
    user = SimpleNamespace(organisation_id=uuid4(), user_id=uuid4())
    winner = ChatMessageJob(
        job_type="whatsapp_chat_response",
        chat_type="WHATSAPP_CHAT",
        job_metadata={"idempotency_key": WhatsAppBotService._idempotency_key(_message())},
    )
    session = _DurableJobSession([winner])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)

    result = await service._create_or_get_durable_job(
        user=user,
        message=_message(),
        attachment_refs=[],
        dispatch_envelope={},
    )

    assert result is winner
    assert session.commit_count == 0
    assert session.added == []
    from sqlalchemy.dialects import postgresql

    lookup_sql = str(
        session.executed[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "chat_message_jobs.idempotency_key" in lookup_sql
    assert "job_metadata" not in lookup_sql.split("WHERE", maxsplit=1)[1]


@pytest.mark.asyncio
async def test_durable_job_unique_race_reloads_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import IntegrityError
    from src.domain.models.organisation_tables.job_models import ChatMessageJob

    service = WhatsAppBotService()
    user = SimpleNamespace(organisation_id=uuid4(), user_id=uuid4())
    winner = ChatMessageJob(
        job_type="whatsapp_chat_response",
        chat_type="WHATSAPP_CHAT",
        job_metadata={"idempotency_key": WhatsAppBotService._idempotency_key(_message())},
    )
    losing_session = _DurableJobSession(
        [None, None, None],
        commit_error=IntegrityError("insert", {}, RuntimeError("duplicate")),
    )
    winning_session = _DurableJobSession([winner])
    sessions_to_yield = iter([losing_session, winning_session])

    async def sessions(_org_id):
        yield next(sessions_to_yield)

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)

    result = await service._create_or_get_durable_job(
        user=user,
        message=_message(),
        attachment_refs=[],
        dispatch_envelope={},
    )

    assert result is winner
    assert losing_session.rollback_count == 1


@pytest.mark.asyncio
async def test_enqueue_whatsapp_job_serializes_the_stored_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WhatsApp must publish its durable envelope with the shared dispatcher deadline."""
    from src.service.bots import whatsapp_bot_service

    job_id = uuid4()
    envelope = {
        "organisation_id": str(uuid4()),
        "user_id": str(uuid4()),
        "message_text": "Where is the proposal?",
        "provider": "whatsapp",
        "reply_context": {
            "phone_number_id": "phone-123",
            "recipient_wa_id": "15551234567",
            "incoming_message_id": "wamid.1",
        },
        "bot_context": {"conversation_type": "personal"},
        "user_email": "test@example.com",
    }
    captured: dict[str, object] = {}

    async def _send_task(name, args, kwargs, queue, routing_key, timeout_seconds=None):
        captured.update(
            name=name,
            args=args,
            kwargs=kwargs,
            queue=queue,
            routing_key=routing_key,
            timeout_seconds=timeout_seconds,
        )
        return "task-123"

    monkeypatch.setattr("src.workers.chat_worker.client.send_chat_task_async", _send_task)

    task_id = await whatsapp_bot_service.enqueue_whatsapp_job(
        job_id=job_id,
        envelope=envelope,
    )

    assert task_id == "task-123"
    assert captured == {
        "name": "chat.process_bot_message",
        "args": [
            envelope["organisation_id"],
            envelope["user_id"],
            envelope["message_text"],
            "whatsapp",
            envelope["reply_context"],
        ],
        "kwargs": {
            "user_email": "test@example.com",
            "bot_context": envelope["bot_context"],
            "job_id": str(job_id),
        },
        "queue": "chat_messages",
        "routing_key": "chat_messages",
        "timeout_seconds": 6.0,
    }


@pytest.mark.asyncio
async def test_enqueue_whatsapp_job_retires_a_blocked_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WhatsApp must reach the dispatcher's timeout so blocked capacity is replaced."""
    from src.infra.messaging import task_dispatcher
    from src.service.bots import whatsapp_bot_service

    executor = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    started = threading.Event()
    replacement_executor = None

    def _blocked_send(*_args, **_kwargs):
        started.set()
        release.wait()
        return "late-task-id"

    monkeypatch.setattr(task_dispatcher, "_executor", executor)
    monkeypatch.setattr(task_dispatcher, "_executor_generation", 0)
    monkeypatch.setattr(task_dispatcher, "_generation_inflight", {})
    monkeypatch.setattr(task_dispatcher, "_retired_generations", set())
    monkeypatch.setattr(task_dispatcher, "send_task", _blocked_send)
    monkeypatch.setattr(
        whatsapp_bot_service,
        "DEFAULT_TASK_PUBLISH_TIMEOUT_SECONDS",
        0.05,
    )

    try:
        with pytest.raises(task_dispatcher.TaskPublishTimeoutError):
            await whatsapp_bot_service.enqueue_whatsapp_job(
                job_id=uuid4(),
                envelope={
                    "organisation_id": str(uuid4()),
                    "user_id": str(uuid4()),
                    "message_text": "hello",
                    "reply_context": {},
                    "bot_context": {},
                },
            )

        replacement_executor = task_dispatcher._executor
        assert started.is_set()
        assert replacement_executor is not executor
        assert task_dispatcher._executor_generation == 1
        assert task_dispatcher._retired_generations == {0}
    finally:
        release.set()
        await asyncio.sleep(0)
        executor.shutdown(wait=True)
        if replacement_executor is not None and replacement_executor is not executor:
            replacement_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_claim_pending_whatsapp_job_persists_enqueue_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.domain.models.organisation_tables.job_models import JobStatus

    service = WhatsAppBotService()
    organisation_id = uuid4()
    job_id = uuid4()
    envelope = {"organisation_id": str(organisation_id)}
    job = SimpleNamespace(
        status=JobStatus.PENDING,
        job_metadata={
            "enqueue_status": "pending",
            "enqueue_attempts": 0,
            "dispatch_envelope": envelope,
        },
        error_message=None,
    )
    session = _DurableJobSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)

    claim = await service._claim_job_for_enqueue(organisation_id, job_id)

    assert claim == (envelope, 1)
    assert job.job_metadata["enqueue_status"] == "enqueuing"
    assert job.job_metadata["enqueue_attempts"] == 1
    assert session.commit_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ["in_progress", "completed", "failed"])
async def test_claim_does_not_republish_active_or_terminal_job(
    monkeypatch: pytest.MonkeyPatch,
    job_status: str,
) -> None:
    service = WhatsAppBotService()
    job = SimpleNamespace(
        status=job_status,
        job_metadata={
            "enqueue_status": "failed",
            "enqueue_attempts": 1,
            "dispatch_envelope": {"organisation_id": str(uuid4())},
        },
    )
    session = _DurableJobSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)

    assert await service._claim_job_for_enqueue(uuid4(), uuid4()) is None
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_claim_does_not_republish_already_enqueued_pending_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    job = SimpleNamespace(
        status="pending",
        job_metadata={
            "enqueue_status": "enqueued",
            "enqueue_attempts": 1,
            "dispatch_envelope": {"organisation_id": str(uuid4())},
        },
    )
    session = _DurableJobSession([job])

    async def sessions(_org_id):
        yield session

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)

    assert await service._claim_job_for_enqueue(uuid4(), uuid4()) is None
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_enqueue_success_records_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.service.bots import whatsapp_bot_service

    service = WhatsAppBotService()
    organisation_id = uuid4()
    job_id = uuid4()
    envelope = {"organisation_id": str(organisation_id)}
    record_mock = AsyncMock()
    monkeypatch.setattr(
        service,
        "_claim_job_for_enqueue",
        AsyncMock(return_value=(envelope, 2)),
    )
    monkeypatch.setattr(service, "_record_enqueue_result", record_mock)
    monkeypatch.setattr(
        whatsapp_bot_service,
        "enqueue_whatsapp_job",
        AsyncMock(return_value="task-123"),
    )

    assert await service._enqueue_durable_job(organisation_id, job_id) is True
    record_mock.assert_awaited_once_with(
        organisation_id,
        job_id,
        attempt=2,
        task_id="task-123",
        error=None,
    )


@pytest.mark.asyncio
async def test_enqueue_failure_records_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.service.bots import whatsapp_bot_service

    service = WhatsAppBotService()
    organisation_id = uuid4()
    job_id = uuid4()
    envelope = {"organisation_id": str(organisation_id)}
    record_mock = AsyncMock()
    monkeypatch.setattr(
        service,
        "_claim_job_for_enqueue",
        AsyncMock(return_value=(envelope, 3)),
    )
    monkeypatch.setattr(service, "_record_enqueue_result", record_mock)
    monkeypatch.setattr(
        whatsapp_bot_service,
        "enqueue_whatsapp_job",
        AsyncMock(side_effect=RuntimeError("redis unavailable")),
    )

    assert await service._enqueue_durable_job(organisation_id, job_id) is False
    record_mock.assert_awaited_once_with(
        organisation_id,
        job_id,
        attempt=3,
        task_id=None,
        error="redis unavailable",
    )


@pytest.mark.asyncio
async def test_record_enqueue_failure_marks_job_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WhatsAppBotService()
    job = SimpleNamespace(
        status=JobStatus.PENDING,
        error_message=None,
        job_metadata={"enqueue_attempts": 1},
    )

    class Session:
        async def get(self, *_args):
            return job

        def add(self, _value):
            return None

        async def commit(self):
            return None

    async def sessions(_org_id):
        yield Session()

    monkeypatch.setattr("src.infra.database.sessions.acquire_org_async_session", sessions)

    await service._record_enqueue_result(
        uuid4(),
        uuid4(),
        attempt=1,
        task_id=None,
        error="redis unavailable",
    )

    assert job.status == JobStatus.FAILED
    assert job.error_message == "WhatsApp task publication failed"
    assert job.job_metadata["enqueue_status"] == "failed"


class _SendResponse:
    def __init__(self, status: int, *, payload=None, body: str = ""):
        self.status = status
        self.payload = payload or {}
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload

    async def text(self):
        return self.body


class _SendSession:
    response = _SendResponse(200)
    error: Exception | None = None
    timeout = None

    def __init__(self, *, timeout=None):
        type(self).timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_whatsapp_send_uses_bounded_http_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        whatsapp_config,
        "get_access_token",
        AsyncMock(return_value="wa-token"),
    )
    _SendSession.error = None
    _SendSession.response = _SendResponse(
        200,
        payload={"messages": [{"id": "wamid.outbound"}]},
    )
    _SendSession.timeout = None
    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.aiohttp.ClientSession",
        _SendSession,
    )

    result = await WhatsAppBotService().send_text_message_result(
        "phone-123",
        "15551234567",
        "hello",
    )

    assert result.accepted is True
    assert _SendSession.timeout is not None
    assert _SendSession.timeout.total < 120


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [(429, "retryable"), (500, "retryable"), (401, "terminal"), (400, "terminal")],
)
async def test_whatsapp_send_classifies_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_kind: str,
) -> None:
    monkeypatch.setattr(
        whatsapp_config,
        "get_access_token",
        AsyncMock(return_value="wa-token"),
    )
    _SendSession.error = None
    _SendSession.response = _SendResponse(
        status_code,
        body='{"error":{"message":"provider rejected request"}}',
    )
    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.aiohttp.ClientSession",
        _SendSession,
    )

    result = await WhatsAppBotService().send_text_message_result(
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="hello",
    )

    assert result.accepted is False
    assert result.failure_kind == expected_kind
    assert result.status_code == status_code
    assert "provider rejected request" in result.error_message


@pytest.mark.asyncio
async def test_whatsapp_send_accepts_2xx_with_provider_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        whatsapp_config,
        "get_access_token",
        AsyncMock(return_value="wa-token"),
    )
    _SendSession.error = None
    _SendSession.response = _SendResponse(
        200,
        payload={"messages": [{"id": "wamid.outbound"}]},
    )
    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.aiohttp.ClientSession",
        _SendSession,
    )

    result = await WhatsAppBotService().send_text_message_result(
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="hello",
    )

    assert result.accepted is True
    assert result.provider_message_id == "wamid.outbound"
    assert result.failure_kind is None


@pytest.mark.asyncio
async def test_whatsapp_send_classifies_network_failure_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        whatsapp_config,
        "get_access_token",
        AsyncMock(return_value="wa-token"),
    )
    _SendSession.error = RuntimeError("connection reset")
    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.aiohttp.ClientSession",
        _SendSession,
    )

    result = await WhatsAppBotService().send_text_message_result(
        phone_number_id="phone-123",
        recipient_wa_id="15551234567",
        text="hello",
    )

    assert result.accepted is False
    assert result.failure_kind == "ambiguous"
    assert "connection reset" in result.error_message
