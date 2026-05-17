"""Regression coverage for HubSpot company webhook handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot.companies.handler import (
    HubSpotCompanyWebhookHandler,
)
from src.service.integrations.webhooks.hubspot.companies.subscriptions import (
    COMPANY_CREATION,
    COMPANY_DELETION,
    COMPANY_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent


def test_single_company_fetcher_uses_default_properties() -> None:
    """Single company fetches reuse the curated full-sync company property set."""
    from src.workers.integrations_worker.providers.crm.hubspot.companies_fetcher import (
        HUBSPOT_COMPANY_PROPERTIES,
    )
    from src.workers.integrations_worker.providers.crm.hubspot.single_company_fetcher import (
        HubSpotSingleCompanyFetcher,
    )

    fetcher = HubSpotSingleCompanyFetcher(token_info=object(), company_id="123")  # type: ignore[arg-type]

    assert fetcher._properties == tuple(sorted(set(HUBSPOT_COMPANY_PROPERTIES)))


def test_single_company_fetcher_requires_properties() -> None:
    """Single company fetches fail fast before persisting partial snapshots."""
    from src.workers.integrations_worker.providers.crm.hubspot.single_company_fetcher import (
        HubSpotSingleCompanyFetcher,
    )

    with pytest.raises(ValueError, match="requires a non-empty properties list"):
        HubSpotSingleCompanyFetcher(
            token_info=object(),  # type: ignore[arg-type]
            company_id="123",
            properties=(),
        )


@pytest.mark.asyncio
async def test_company_property_change_processes_any_subscribed_property(monkeypatch) -> None:
    """Subscribed property changes route to the full company snapshot upsert path."""
    upsert = AsyncMock()
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotCompanyWebhookHandler, "_handle_upsert", upsert)
    monkeypatch.setattr(HubSpotCompanyWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=COMPANY_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "name"},
    )

    await HubSpotCompanyWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)
    deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_company_deletion_routes_to_deletion_handler(monkeypatch) -> None:
    """Deletion webhooks route directly to local company soft deletion."""
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotCompanyWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=COMPANY_DELETION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotCompanyWebhookHandler.handle(event)

    deletion.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_company_creation_fetches_full_company_and_persists(monkeypatch) -> None:
    """Company creation fetches one HubSpot company and reuses company sync logic."""
    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    expected_token_info = object()
    raw_company = {"id": "456", "properties": {"name": "Acme"}}
    persist_calls: list[dict[str, object]] = []

    async def fake_targets(portal_id: str):
        """Yield the org/integration target resolved from the webhook portal."""
        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        """Test double for the existing company sync service."""

        async def _load_token_info(self, loaded_org_id, loaded_integration_id):
            assert loaded_org_id == organisation_id
            assert loaded_integration_id == integration_id
            return expected_token_info

        async def persist_raw_company(self, **kwargs) -> None:
            persist_calls.append(kwargs)

    class FakeFetcher:
        """Test double for the single company provider fetch."""

        def __init__(self, *, token_info: object, company_id: str) -> None:
            assert token_info is expected_token_info
            assert company_id == "456"

        async def fetch(self) -> dict[str, object]:
            """Return the fetched HubSpot company payload for persistence."""
            return raw_company

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.companies.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.companies.handler.HubSpotCompanyRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.companies.handler.HubSpotSingleCompanyFetcher",
        FakeFetcher,
    )
    event = HubSpotWebhookEvent(
        subscription_type=COMPANY_CREATION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotCompanyWebhookHandler.handle(event)

    assert persist_calls == [
        {
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "raw_company": raw_company,
        }
    ]


@pytest.mark.asyncio
async def test_company_upsert_404_soft_deletes_local_company(monkeypatch) -> None:
    """A provider 404 during company upsert becomes a local soft delete."""
    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    soft_delete = AsyncMock()

    async def fake_targets(portal_id: str):
        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        async def _load_token_info(self, loaded_org_id, loaded_integration_id):
            return object()

    class FakeFetcher:
        def __init__(self, *, token_info: object, company_id: str) -> None:
            assert company_id == "456"

        async def fetch(self) -> None:
            return None

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.companies.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.companies.handler.HubSpotCompanyRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.companies.handler.HubSpotSingleCompanyFetcher",
        FakeFetcher,
    )
    monkeypatch.setattr(HubSpotCompanyWebhookHandler, "_soft_delete_company", soft_delete)
    event = HubSpotWebhookEvent(
        subscription_type=COMPANY_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "domain"},
    )

    await HubSpotCompanyWebhookHandler.handle(event)

    soft_delete.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="456",
    )
