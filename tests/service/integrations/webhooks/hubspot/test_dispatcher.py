from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot.constants import CONTACT_PRIVACY_DELETION
from src.service.integrations.webhooks.hubspot.dispatcher import HubSpotWebhookDispatcher
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent


@pytest.mark.asyncio
async def test_dispatcher_routes_supported_hubspot_event(monkeypatch) -> None:
    handler = AsyncMock()
    monkeypatch.setitem(
        HubSpotWebhookDispatcher._handlers,
        CONTACT_PRIVACY_DELETION,
        handler,
    )
    event = HubSpotWebhookEvent(
        subscription_type=CONTACT_PRIVACY_DELETION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    assert await HubSpotWebhookDispatcher.dispatch(event) is True
    handler.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_dispatcher_ignores_unsupported_hubspot_event() -> None:
    event = HubSpotWebhookEvent(
        subscription_type="deal.propertyChange",
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    assert await HubSpotWebhookDispatcher.dispatch(event) is False
