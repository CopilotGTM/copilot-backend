"""Regression coverage for HubSpot contact webhook handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot.contacts.handler import (
    HubSpotContactWebhookHandler,
)
from src.service.integrations.webhooks.hubspot.contacts.subscriptions import (
    CONTACT_CREATION,
    CONTACT_DELETION,
    CONTACT_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent


def test_single_contact_fetcher_uses_default_properties() -> None:
    """Single contact fetches reuse the curated full-sync contact property set."""
    from src.workers.integrations_worker.providers.crm.hubspot.contacts_fetcher import (
        HUBSPOT_CONTACT_PROPERTIES,
    )
    from src.workers.integrations_worker.providers.crm.hubspot.single_contact_fetcher import (
        HubSpotSingleContactFetcher,
    )

    fetcher = HubSpotSingleContactFetcher(token_info=object(), contact_id="123")  # type: ignore[arg-type]

    assert fetcher._properties == tuple(sorted(set(HUBSPOT_CONTACT_PROPERTIES)))


def test_single_contact_fetcher_requires_properties() -> None:
    """Single contact fetches fail fast before persisting partial snapshots."""
    from src.workers.integrations_worker.providers.crm.hubspot.single_contact_fetcher import (
        HubSpotSingleContactFetcher,
    )

    with pytest.raises(ValueError, match="requires a non-empty properties list"):
        HubSpotSingleContactFetcher(
            token_info=object(),  # type: ignore[arg-type]
            contact_id="123",
            properties=(),
        )


@pytest.mark.asyncio
async def test_contact_property_change_routes_to_upsert(monkeypatch) -> None:
    """Regression test for contact property change routes to upsert."""

    upsert = AsyncMock()
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotContactWebhookHandler, "_handle_upsert", upsert)
    monkeypatch.setattr(HubSpotContactWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=CONTACT_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "email"},
    )

    await HubSpotContactWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)
    deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_deletion_routes_to_deletion_handler(monkeypatch) -> None:
    """Regression test for contact deletion routes to deletion handler."""

    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotContactWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=CONTACT_DELETION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotContactWebhookHandler.handle(event)

    deletion.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_contact_creation_fetches_full_contact_and_persists(monkeypatch) -> None:
    """Regression test for contact creation fetches full contact and persists."""

    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    expected_token_info = object()
    raw_contact = {"id": "456", "properties": {"email": "buyer@example.com"}}
    persist_calls: list[dict[str, object]] = []

    async def fake_targets(portal_id: str):
        """Execute fake targets."""

        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        """Test service stub used to capture sync side effects."""

        async def _load_token_info(self, loaded_org_id, loaded_integration_id):
            assert loaded_org_id == organisation_id
            assert loaded_integration_id == integration_id
            return expected_token_info

        async def persist_raw_contact(self, **kwargs) -> None:
            """Persist a webhook contact payload into local CRM tables."""

            persist_calls.append(kwargs)

    class FakeFetcher:
        """Test fetcher stub that returns a synthetic HubSpot contact payload."""

        def __init__(self, *, token_info: object, contact_id: str) -> None:
            assert token_info is expected_token_info
            assert contact_id == "456"

        async def fetch(self) -> dict[str, object]:
            """Execute fetch."""

            return raw_contact

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.contacts.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.contacts.handler.HubSpotContactRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.contacts.handler.HubSpotSingleContactFetcher",
        FakeFetcher,
    )
    event = HubSpotWebhookEvent(
        subscription_type=CONTACT_CREATION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotContactWebhookHandler.handle(event)

    assert persist_calls == [
        {
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "raw_contact": raw_contact,
        }
    ]


@pytest.mark.asyncio
async def test_contact_upsert_404_soft_deletes_local_contact(monkeypatch) -> None:
    """Regression test for contact upsert 404 soft deletes local contact."""

    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    soft_delete = AsyncMock()

    async def fake_targets(portal_id: str):
        """Execute fake targets."""

        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        """Test service stub used to emulate token lookup behavior."""

        async def _load_token_info(self, loaded_org_id, loaded_integration_id):
            return object()

    class FakeFetcher:
        """Test fetcher stub that simulates HubSpot 404 behavior."""

        def __init__(self, *, token_info: object, contact_id: str) -> None:
            assert contact_id == "456"

        async def fetch(self) -> None:
            """Execute fetch."""

            return None

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.contacts.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.contacts.handler.HubSpotContactRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.contacts.handler.HubSpotSingleContactFetcher",
        FakeFetcher,
    )
    monkeypatch.setattr(HubSpotContactWebhookHandler, "_soft_delete_contact", soft_delete)
    event = HubSpotWebhookEvent(
        subscription_type=CONTACT_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "email"},
    )

    await HubSpotContactWebhookHandler.handle(event)

    soft_delete.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="456",
    )
