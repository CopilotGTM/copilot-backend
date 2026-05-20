"""Regression coverage for HubSpot deal webhook handling.

Deal webhooks should resolve the HubSpot portal to an org integration, fetch
the complete deal snapshot for creates/updates, and reuse the existing deal
sync persistence path rather than duplicating writer logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot.deals.handler import (
    HubSpotDealWebhookHandler,
)
from src.service.integrations.webhooks.hubspot.deals.subscriptions import (
    DEAL_CREATION,
    DEAL_DELETION,
    DEAL_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.portal_resolver import (
    integration_matches_hubspot_portal,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent


def test_integration_matches_hubspot_portal_from_metadata() -> None:
    """Portal metadata matches the HubSpot webhook portal id exactly."""
    # Portal metadata is the bridge from HubSpot's webhook payload to our
    # tenant/integration identity.
    integration = SimpleNamespace(connection_metadata={"hubspot_portal_id": "123"})

    assert integration_matches_hubspot_portal(integration, "123") is True
    assert integration_matches_hubspot_portal(integration, "456") is False


def test_integration_without_portal_metadata_does_not_match() -> None:
    """Integrations without portal metadata do not match webhook events."""
    # Missing portal metadata must not become a wildcard match across tenants.
    integration = SimpleNamespace(connection_metadata={})

    assert integration_matches_hubspot_portal(integration, "123") is False


def test_single_deal_fetcher_requires_properties() -> None:
    """Single deal fetches require the full sync property list."""
    # A webhook fetch without a property list would persist a partial deal and
    # diverge from full sync, so construction fails fast.
    from src.workers.integrations_worker.providers.crm.hubspot.single_deal_fetcher import (
        HubSpotSingleDealFetcher,
    )

    with pytest.raises(ValueError, match="requires a non-empty properties list"):
        HubSpotSingleDealFetcher(token_info=object(), deal_id="123", properties=[])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_deal_property_change_processes_any_subscribed_property(monkeypatch) -> None:
    """Subscribed property changes route to the full deal upsert path."""
    # HubSpot subscription filtering happens in the app configuration; once the
    # route receives a subscribed propertyChange, the handler fetches the full deal.
    upsert = AsyncMock()
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotDealWebhookHandler, "_handle_upsert", upsert)
    monkeypatch.setattr(HubSpotDealWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=DEAL_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "hs_lastmodifieddate"},
    )

    await HubSpotDealWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)
    deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_deal_property_change_processes_selected_property(monkeypatch) -> None:
    """Selected property changes share the same full snapshot upsert path."""
    # Selected properties use the same upsert path as generic property changes
    # so the persisted row is always a full refreshed snapshot.
    upsert = AsyncMock()
    monkeypatch.setattr(HubSpotDealWebhookHandler, "_handle_upsert", upsert)
    event = HubSpotWebhookEvent(
        subscription_type=DEAL_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "dealstage"},
    )

    await HubSpotDealWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_deal_deletion_routes_to_deletion_handler(monkeypatch) -> None:
    """Deletion webhooks route directly to local soft deletion."""
    # Deletion webhooks carry no full payload, so they must bypass provider fetch
    # and go straight to the local soft-delete path.
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotDealWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=DEAL_DELETION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotDealWebhookHandler.handle(event)

    deletion.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_deal_creation_fetches_full_deal_and_persists(monkeypatch) -> None:
    """Deal creation fetches the full HubSpot deal and reuses sync persistence."""
    # Creation webhooks only provide object identity; the handler must fetch the
    # full HubSpot deal and pass it to the established sync pipeline.
    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    expected_token_info = object()
    raw_deal = {"id": "456", "properties": {"dealname": "Acme renewal"}}
    persist_calls: list[dict[str, object]] = []

    async def fake_targets(portal_id: str):
        """Yield the org/integration target resolved from the webhook portal."""
        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        """Test double for the existing deal sync service."""

        async def _load_token_info(self, loaded_org_id, loaded_integration_id):
            assert loaded_org_id == organisation_id
            assert loaded_integration_id == integration_id
            return expected_token_info

        async def _build_property_list(self, *, token_info: object) -> list[str]:
            assert token_info is expected_token_info
            return ["dealname", "dealstage"]

        async def _persist_raw_deals(self, **kwargs) -> None:
            persist_calls.append(kwargs)

    class FakeFetcher:
        """Test double for the single deal provider fetch."""

        def __init__(self, *, token_info: object, deal_id: str, properties: list[str]) -> None:
            assert token_info is expected_token_info
            assert deal_id == "456"
            assert properties == ["dealname", "dealstage"]

        async def fetch(self) -> dict[str, object]:
            """Return the fetched HubSpot deal payload for persistence."""
            return raw_deal

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.deals.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.deals.handler.HubSpotDealRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.deals.handler.HubSpotSingleDealFetcher",
        FakeFetcher,
    )
    event = HubSpotWebhookEvent(
        subscription_type=DEAL_CREATION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotDealWebhookHandler.handle(event)

    assert persist_calls == [
        {
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "raw_deals": [raw_deal],
            "property_list_size": 2,
        }
    ]
