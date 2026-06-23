"""Tests for WhatsApp outbound provider helpers."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_send_template_message_builds_cloud_api_payload(monkeypatch):
    """Approved templates are the out-of-window WhatsApp send primitive."""
    from src.service.channels.providers.whatsapp_outbound_provider import (
        WhatsAppOutboundProvider,
    )

    captured: dict[str, object] = {}

    async def _fake_post(phone_number_id: str, payload: dict[str, object]):
        captured["phone_number_id"] = phone_number_id
        captured["payload"] = payload
        return {"messages": [{"id": "wamid.test"}]}

    provider = WhatsAppOutboundProvider()
    monkeypatch.setattr(provider, "_post_message_payload", _fake_post)

    message_id = await provider.send_template_message_with_id(
        phone_number_id="phone-1",
        recipient_wa_id="wa-1",
        template_name="task_reminder",
        language_code="en_US",
        body_parameters=["Follow up with Acme", "https://app.example.test/tasks/1"],
    )

    assert message_id == "wamid.test"
    assert captured == {
        "phone_number_id": "phone-1",
        "payload": {
            "messaging_product": "whatsapp",
            "to": "wa-1",
            "type": "template",
            "template": {
                "name": "task_reminder",
                "language": {"code": "en_US"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": "Follow up with Acme"},
                            {"type": "text", "text": "https://app.example.test/tasks/1"},
                        ],
                    }
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_send_text_message_builds_cloud_api_payload(monkeypatch):
    """Freeform WhatsApp sends are used inside the customer-service window."""
    from src.service.channels.providers.whatsapp_outbound_provider import (
        WhatsAppOutboundProvider,
    )

    captured: dict[str, object] = {}

    async def _fake_post(phone_number_id: str, payload: dict[str, object]):
        captured["phone_number_id"] = phone_number_id
        captured["payload"] = payload
        return {"messages": [{"id": "wamid.freeform"}]}

    provider = WhatsAppOutboundProvider()
    monkeypatch.setattr(provider, "_post_message_payload", _fake_post)

    message_id = await provider.send_text_message_with_id(
        phone_number_id="phone-1",
        recipient_wa_id="wa-1",
        text="Meeting summary is ready: Pricing Review",
    )

    assert message_id == "wamid.freeform"
    assert captured == {
        "phone_number_id": "phone-1",
        "payload": {
            "messaging_product": "whatsapp",
            "to": "wa-1",
            "type": "text",
            "text": {"body": "Meeting summary is ready: Pricing Review"},
        },
    }


def test_whatsapp_outbound_provider_is_exported_from_channels_package():
    from src.service.channels import WhatsAppOutboundProvider

    assert WhatsAppOutboundProvider.__name__ == "WhatsAppOutboundProvider"
