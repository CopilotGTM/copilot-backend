"""Regression coverage for HubSpot webhook dispatch routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot import dispatcher as dispatcher_module
from src.service.integrations.webhooks.hubspot.companies.subscriptions import (
    COMPANY_CREATION,
    COMPANY_DELETION,
    COMPANY_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.deals.subscriptions import (
    DEAL_CREATION,
    DEAL_DELETION,
    DEAL_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.dispatcher import HubSpotWebhookDispatcher
from src.service.integrations.webhooks.hubspot.notes.subscriptions import (
    NOTE_ASSOCIATION_CHANGE,
    NOTE_CREATION,
    NOTE_DELETION,
    NOTE_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.tasks.subscriptions import (
    TASK_ASSOCIATION_CHANGE,
    TASK_CREATION,
    TASK_DELETION,
    TASK_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent


@pytest.mark.asyncio
async def test_dispatcher_routes_supported_hubspot_event(monkeypatch) -> None:
    """Supported HubSpot subscription types call their registered handler."""
    # Supported subscriptions must call the exact registered handler; this is
    # the contract the route relies on after it filters supported event names.
    handler = AsyncMock()
    monkeypatch.setitem(dispatcher_module._DISPATCHER._handlers, DEAL_PROPERTY_CHANGE, handler)
    event = HubSpotWebhookEvent(
        subscription_type=DEAL_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "hs_lastmodifieddate"},
    )

    assert await HubSpotWebhookDispatcher.dispatch(event) is True
    handler.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_dispatcher_ignores_unsupported_hubspot_event() -> None:
    """Unsupported HubSpot subscription types return unhandled without raising."""
    # Unknown HubSpot subscriptions are acknowledged as unhandled instead of
    # raising, which lets newly enabled HubSpot app events be rolled out safely.
    event = HubSpotWebhookEvent(
        subscription_type="deal.merge",
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    assert await HubSpotWebhookDispatcher.dispatch(event) is False


def test_dispatcher_recognizes_hubspot_deal_events() -> None:
    """The dispatcher support check includes all deal webhook subscriptions."""
    # The route uses this support check before enqueueing, so all deal webhook
    # subscriptions added by the rollout must be visible here.
    assert HubSpotWebhookDispatcher.is_supported(DEAL_CREATION) is True
    assert HubSpotWebhookDispatcher.is_supported(DEAL_PROPERTY_CHANGE) is True
    assert HubSpotWebhookDispatcher.is_supported(DEAL_DELETION) is True


def test_dispatcher_recognizes_hubspot_company_events() -> None:
    """The dispatcher support check includes the v1 company webhook subscriptions."""
    assert HubSpotWebhookDispatcher.is_supported(COMPANY_CREATION) is True
    assert HubSpotWebhookDispatcher.is_supported(COMPANY_PROPERTY_CHANGE) is True
    assert HubSpotWebhookDispatcher.is_supported(COMPANY_DELETION) is True


def test_dispatcher_recognizes_hubspot_note_events() -> None:
    """The dispatcher support check includes normalized note webhook subscriptions."""
    assert HubSpotWebhookDispatcher.is_supported(NOTE_CREATION) is True
    assert HubSpotWebhookDispatcher.is_supported(NOTE_PROPERTY_CHANGE) is True
    assert HubSpotWebhookDispatcher.is_supported(NOTE_ASSOCIATION_CHANGE) is True
    assert HubSpotWebhookDispatcher.is_supported(NOTE_DELETION) is True


def test_dispatcher_recognizes_hubspot_task_events() -> None:
    """The dispatcher support check includes normalized task webhook subscriptions."""
    assert HubSpotWebhookDispatcher.is_supported(TASK_CREATION) is True
    assert HubSpotWebhookDispatcher.is_supported(TASK_PROPERTY_CHANGE) is True
    assert HubSpotWebhookDispatcher.is_supported(TASK_ASSOCIATION_CHANGE) is True
    assert HubSpotWebhookDispatcher.is_supported(TASK_DELETION) is True
