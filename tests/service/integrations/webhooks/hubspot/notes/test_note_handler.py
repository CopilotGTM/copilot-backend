"""Regression coverage for HubSpot note webhook handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot.notes.handler import HubSpotNoteWebhookHandler
from src.service.integrations.webhooks.hubspot.notes.subscriptions import (
    NOTE_ASSOCIATION_CHANGE,
    NOTE_CREATION,
    NOTE_DELETION,
    NOTE_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent


@pytest.mark.asyncio
async def test_note_property_change_routes_to_upsert(monkeypatch) -> None:
    """Property changes fetch the latest full note snapshot before writing."""
    upsert = AsyncMock()
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotNoteWebhookHandler, "_handle_upsert", upsert)
    monkeypatch.setattr(HubSpotNoteWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=NOTE_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "hs_note_body"},
    )

    await HubSpotNoteWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)
    deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_association_change_routes_to_upsert(monkeypatch) -> None:
    """Association changes refresh the full note because links affect note value."""
    upsert = AsyncMock()
    monkeypatch.setattr(HubSpotNoteWebhookHandler, "_handle_upsert", upsert)
    event = HubSpotWebhookEvent(
        subscription_type=NOTE_ASSOCIATION_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"toObjectTypeId": "0-3"},
    )

    await HubSpotNoteWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_note_deletion_routes_to_deletion_handler(monkeypatch) -> None:
    """Deletion webhooks bypass HubSpot fetches and tombstone local notes."""
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotNoteWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=NOTE_DELETION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotNoteWebhookHandler.handle(event)

    deletion.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_note_creation_fetches_full_note_and_persists(monkeypatch) -> None:
    """Creation webhooks fetch one full note and reuse existing note persistence."""
    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    expected_token_info = object()
    raw_note = {"id": "456", "properties": {"hs_note_body": "Buyer note"}}
    persist_calls: list[dict[str, object]] = []

    async def fake_targets(portal_id: str):
        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        async def _load_context(self, loaded_org_id, loaded_integration_id):
            assert loaded_org_id == organisation_id
            assert loaded_integration_id == integration_id
            return SimpleNamespace(token_info=expected_token_info)

        async def persist_raw_note(self, **kwargs) -> None:
            persist_calls.append(kwargs)

    class FakeFetcher:
        def __init__(self, *, token_info: object, note_id: str) -> None:
            assert token_info is expected_token_info
            assert note_id == "456"

        async def fetch(self) -> dict[str, object]:
            return raw_note

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.notes.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.notes.handler.HubSpotNoteRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.notes.handler.HubSpotSingleNoteFetcher",
        FakeFetcher,
    )
    event = HubSpotWebhookEvent(
        subscription_type=NOTE_CREATION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotNoteWebhookHandler.handle(event)

    assert persist_calls == [
        {
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "raw_note": raw_note,
        }
    ]


@pytest.mark.asyncio
async def test_note_upsert_404_soft_deletes_matching_note(monkeypatch) -> None:
    """A missing provider note from an upsert event becomes a local tombstone."""
    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    expected_token_info = object()
    soft_delete = AsyncMock()

    async def fake_targets(portal_id: str):
        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        async def _load_context(self, loaded_org_id, loaded_integration_id):
            assert loaded_org_id == organisation_id
            assert loaded_integration_id == integration_id
            return SimpleNamespace(token_info=expected_token_info)

    class FakeFetcher:
        def __init__(self, *, token_info: object, note_id: str) -> None:
            assert token_info is expected_token_info
            assert note_id == "456"

        async def fetch(self) -> None:
            return None

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.notes.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.notes.handler.HubSpotNoteRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.notes.handler.HubSpotSingleNoteFetcher",
        FakeFetcher,
    )
    monkeypatch.setattr(HubSpotNoteWebhookHandler, "_soft_delete_note", soft_delete)
    event = HubSpotWebhookEvent(
        subscription_type=NOTE_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotNoteWebhookHandler.handle(event)

    soft_delete.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="456",
    )
