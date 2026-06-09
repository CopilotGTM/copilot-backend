import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.adapters import ChannelPayload
from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.service.channels.outbound_channel_service import (
    OutboundChannelService,
    OutboundDeliveryTarget,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_list_user_channels_includes_display_metadata() -> None:
    channels = await OutboundChannelService().list_user_channels(
        _Session([]),
        uuid4(),
        uuid4(),
    )

    by_channel = {item.channel: item for item in channels}
    assert by_channel["teams"].label == "Microsoft Teams"
    assert by_channel["teams"].icon_url == "/integrations/microsoft-teams.png"
    assert by_channel["slack"].label == "Slack"
    assert by_channel["slack"].icon_url == "/integrations/slack.png"
    assert by_channel["whatsapp"].label == "WhatsApp"
    assert (
        by_channel["whatsapp"].icon_url
        == "https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg"
    )


@pytest.mark.asyncio
async def test_list_user_channels_normalizes_slack_and_marks_ready_channels() -> None:
    org_id = uuid4()
    user_id = uuid4()
    slack_mapping_id = uuid4()
    session = _Session(
        [
            SimpleNamespace(
                provider=ExternalIdentityProvider.SLACK,
                mapping_id=slack_mapping_id,
                conversation_context={"channel_id": "C123"},
                external_tenant_id="T123",
            ),
            SimpleNamespace(
                provider=ExternalIdentityProvider.WHATSAPP,
                mapping_id=uuid4(),
                conversation_context={"phone_number_id": "phone-1"},
                external_tenant_id="phone-1",
            ),
        ]
    )

    channels = await OutboundChannelService().list_user_channels(session, org_id, user_id)

    by_channel = {item.channel: item for item in channels}
    assert by_channel["slack"].available is True
    assert by_channel["slack"].mapping_id == slack_mapping_id
    assert by_channel["whatsapp"].available is False
    assert by_channel["teams"].available is False
    assert "external_identity_mappings.organisation_id" in str(session.statements[0])
    assert "external_identity_mappings.user_id" in str(session.statements[0])
    assert "external_identity_mappings.is_active" in str(session.statements[0])


@pytest.mark.asyncio
async def test_list_user_channels_reports_ready_mapping_id_when_first_mapping_is_stale() -> None:
    org_id = uuid4()
    user_id = uuid4()
    stale_mapping_id = uuid4()
    ready_mapping_id = uuid4()
    session = _Session(
        [
            SimpleNamespace(
                provider=ExternalIdentityProvider.SLACK,
                mapping_id=stale_mapping_id,
                conversation_context={"channel_id": ""},
                external_tenant_id="",
            ),
            SimpleNamespace(
                provider=ExternalIdentityProvider.SLACK,
                mapping_id=ready_mapping_id,
                conversation_context={"channel_id": "C123"},
                external_tenant_id="T123",
            ),
        ]
    )

    channels = await OutboundChannelService().list_user_channels(session, org_id, user_id)

    slack_channel = {item.channel: item for item in channels}["slack"]
    assert slack_channel.available is True
    assert slack_channel.mapping_id == ready_mapping_id


@pytest.mark.asyncio
async def test_resolve_user_channel_returns_normalized_target() -> None:
    mapping_id = uuid4()
    session = _Session(
        [
            SimpleNamespace(
                mapping_id=mapping_id,
                conversation_context={"channel_id": "C123"},
                external_tenant_id="T123",
            )
        ]
    )

    target = await OutboundChannelService().resolve_user_channel(
        session,
        uuid4(),
        uuid4(),
        "slack",
    )

    assert target == OutboundDeliveryTarget(
        channel="slack",
        mapping_id=mapping_id,
        delivery_context={"channel_id": "C123", "team_id": "T123"},
    )


@pytest.mark.asyncio
async def test_resolve_user_channel_skips_stale_active_rows() -> None:
    ready_mapping_id = uuid4()
    session = _Session(
        [
            SimpleNamespace(
                mapping_id=uuid4(),
                conversation_context={"channel_id": ""},
                external_tenant_id="",
            ),
            SimpleNamespace(
                mapping_id=ready_mapping_id,
                conversation_context={"channel_id": "C123"},
                external_tenant_id="T123",
            ),
        ]
    )

    target = await OutboundChannelService().resolve_user_channel(
        session,
        uuid4(),
        uuid4(),
        "slack",
    )

    assert target == OutboundDeliveryTarget(
        channel="slack",
        mapping_id=ready_mapping_id,
        delivery_context={"channel_id": "C123", "team_id": "T123"},
    )


@pytest.mark.asyncio
async def test_resolve_user_channel_returns_none_for_incomplete_context() -> None:
    session = _Session(
        [
            SimpleNamespace(
                mapping_id=uuid4(),
                conversation_context={"phone_number_id": "phone-1"},
                external_tenant_id="phone-1",
            )
        ]
    )

    target = await OutboundChannelService().resolve_user_channel(
        session,
        uuid4(),
        uuid4(),
        "whatsapp",
    )

    assert target is None


@pytest.mark.asyncio
async def test_resolve_user_channel_treats_malformed_context_as_unavailable() -> None:
    session = _Session(
        [
            SimpleNamespace(
                mapping_id=uuid4(),
                conversation_context=["not", "a", "dict"],
                external_tenant_id="T123",
            )
        ]
    )

    target = await OutboundChannelService().resolve_user_channel(
        session,
        uuid4(),
        uuid4(),
        "slack",
    )

    assert target is None


@pytest.mark.asyncio
async def test_send_text_to_all_available_returns_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []

    class FakeTeamsBotService:
        async def send_proactive_message(
            self, service_url: str, conversation_id: str, message: str
        ):
            sent.append(("teams", message))
            return True

    class FakeSlackBotService:
        async def _send_message(self, team_id: str, channel_id: str, text: str, thread_ts=None):
            sent.append(("slack", text))
            return False

    monkeypatch.setattr("src.service.bots.teams_bot_service.TeamsBotService", FakeTeamsBotService)
    monkeypatch.setattr("src.service.bots.slack_bot_service.SlackBotService", FakeSlackBotService)

    service = OutboundChannelService()
    results = await service.send_text_to_targets(
        [
            OutboundDeliveryTarget(
                channel="teams",
                mapping_id=uuid4(),
                delivery_context={"service_url": "https://service", "conversation_id": "conv"},
            ),
            OutboundDeliveryTarget(
                channel="slack",
                mapping_id=uuid4(),
                delivery_context={"team_id": "T123", "channel_id": "C123"},
            ),
        ],
        "Meeting prep is ready.",
    )

    assert [(result.channel, result.success) for result in results] == [
        ("teams", True),
        ("slack", False),
    ]
    assert sent == [("teams", "Meeting prep is ready."), ("slack", "Meeting prep is ready.")]


@pytest.mark.asyncio
async def test_send_text_rejects_incomplete_target_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class FakeTeamsBotService:
        async def send_proactive_message(
            self, service_url: str, conversation_id: str, message: str
        ):
            nonlocal called
            called = True
            return True

    monkeypatch.setattr("src.service.bots.teams_bot_service.TeamsBotService", FakeTeamsBotService)

    result = await OutboundChannelService().send_text(
        OutboundDeliveryTarget(
            channel="teams",
            mapping_id=uuid4(),
            delivery_context={"service_url": "", "conversation_id": "conv"},
        ),
        "Meeting prep is ready.",
    )

    assert result.success is False
    assert result.error == "Delivery context is incomplete"
    assert called is False


@pytest.mark.asyncio
async def test_send_text_rejects_empty_text_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class FakeWhatsAppBotService:
        async def send_text_message(self, phone_number_id: str, recipient_wa_id: str, text: str):
            nonlocal called
            called = True
            return True

    monkeypatch.setattr(
        "src.service.bots.whatsapp_bot_service.WhatsAppBotService",
        FakeWhatsAppBotService,
    )

    result = await OutboundChannelService().send_text(
        OutboundDeliveryTarget(
            channel="whatsapp",
            mapping_id=uuid4(),
            delivery_context={"phone_number_id": "phone-1", "recipient_wa_id": "wa-1"},
        ),
        "   ",
    )

    assert result.success is False
    assert result.error == "Message text is empty"
    assert called is False


@pytest.mark.asyncio
async def test_send_text_rejects_unsupported_target() -> None:
    result = await OutboundChannelService().send_text(
        OutboundDeliveryTarget(
            channel="email",
            mapping_id=uuid4(),
            delivery_context={"address": "person@example.com"},
        ),
        "Meeting prep is ready.",
    )

    assert result.success is False
    assert result.error == "Unsupported outbound channel"


@pytest.mark.asyncio
async def test_send_text_does_not_return_raw_provider_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeSlackBotService:
        async def _send_message(self, team_id: str, channel_id: str, text: str, thread_ts=None):
            raise RuntimeError("secret-token-for-provider")

    monkeypatch.setattr("src.service.bots.slack_bot_service.SlackBotService", FakeSlackBotService)

    caplog.set_level(logging.WARNING)
    result = await OutboundChannelService().send_text(
        OutboundDeliveryTarget(
            channel="slack",
            mapping_id=uuid4(),
            delivery_context={"team_id": "T123", "channel_id": "C123"},
        ),
        "Meeting prep is ready.",
    )

    assert result.success is False
    assert result.error == "RuntimeError"
    assert "secret-token-for-provider" not in caplog.text


@pytest.mark.asyncio
async def test_send_payload_uses_channel_specific_payload_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeSlackBotService:
        async def send_block_kit_message(
            self,
            team_id: str,
            channel_id: str,
            blocks: list[dict],
            text_fallback: str,
            attachments=None,
            thread_ts=None,
        ):
            captured["team_id"] = team_id
            captured["channel_id"] = channel_id
            captured["blocks"] = blocks
            captured["text_fallback"] = text_fallback
            captured["thread_ts"] = thread_ts
            return True

    monkeypatch.setattr("src.service.bots.slack_bot_service.SlackBotService", FakeSlackBotService)

    result = await OutboundChannelService().send_payload(
        OutboundDeliveryTarget(
            channel="slack",
            mapping_id=uuid4(),
            delivery_context={"team_id": "T123", "channel_id": "C123", "thread_ts": "123.456"},
        ),
        ChannelPayload(
            type="message",
            content={"blocks": [{"type": "section"}], "text": "fallback"},
            text_fallback="fallback",
        ),
    )

    assert result.success is True
    assert captured == {
        "team_id": "T123",
        "channel_id": "C123",
        "blocks": [{"type": "section"}],
        "text_fallback": "fallback",
        "thread_ts": "123.456",
    }
