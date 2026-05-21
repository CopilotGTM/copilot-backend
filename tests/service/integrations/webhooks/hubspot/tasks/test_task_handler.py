"""Regression coverage for HubSpot task webhook handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot.tasks.handler import HubSpotTaskWebhookHandler
from src.service.integrations.webhooks.hubspot.tasks.subscriptions import (
    TASK_ASSOCIATION_CHANGE,
    TASK_CREATION,
    TASK_DELETION,
    TASK_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent


@pytest.mark.asyncio
async def test_task_property_change_routes_to_upsert(monkeypatch) -> None:
    """Property changes fetch the latest full task snapshot before writing."""
    upsert = AsyncMock()
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotTaskWebhookHandler, "_handle_upsert", upsert)
    monkeypatch.setattr(HubSpotTaskWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=TASK_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"propertyName": "hs_task_status"},
    )

    await HubSpotTaskWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)
    deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_association_change_routes_to_upsert(monkeypatch) -> None:
    """Association changes refresh the full task because links affect task value."""
    upsert = AsyncMock()
    monkeypatch.setattr(HubSpotTaskWebhookHandler, "_handle_upsert", upsert)
    event = HubSpotWebhookEvent(
        subscription_type=TASK_ASSOCIATION_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={"toObjectTypeId": "0-3"},
    )

    await HubSpotTaskWebhookHandler.handle(event)

    upsert.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_task_deletion_routes_to_deletion_handler(monkeypatch) -> None:
    """Deletion webhooks bypass HubSpot fetches and tombstone local tasks."""
    deletion = AsyncMock()
    monkeypatch.setattr(HubSpotTaskWebhookHandler, "_handle_deletion", deletion)
    event = HubSpotWebhookEvent(
        subscription_type=TASK_DELETION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotTaskWebhookHandler.handle(event)

    deletion.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_task_creation_fetches_full_task_and_persists(monkeypatch) -> None:
    """Creation webhooks fetch one full task and reuse existing task persistence."""
    organisation_id = "00000000-0000-0000-0000-000000000001"
    integration_id = "00000000-0000-0000-0000-000000000002"
    expected_token_info = object()
    raw_task = {"id": "456", "properties": {"hs_task_subject": "Follow up"}}
    persist_calls: list[dict[str, object]] = []

    async def fake_targets(portal_id: str):
        assert portal_id == "123"
        yield SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    class FakeService:
        async def _load_context(self, loaded_org_id, loaded_integration_id):
            assert loaded_org_id == organisation_id
            assert loaded_integration_id == integration_id
            return SimpleNamespace(token_info=expected_token_info)

        async def persist_raw_task(self, **kwargs) -> None:
            persist_calls.append(kwargs)

    class FakeFetcher:
        def __init__(self, *, token_info: object, task_id: str) -> None:
            assert token_info is expected_token_info
            assert task_id == "456"

        async def fetch(self) -> dict[str, object]:
            return raw_task

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.tasks.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.tasks.handler.HubSpotTaskRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.tasks.handler.HubSpotSingleTaskFetcher",
        FakeFetcher,
    )
    event = HubSpotWebhookEvent(
        subscription_type=TASK_CREATION,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotTaskWebhookHandler.handle(event)

    assert persist_calls == [
        {
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "raw_task": raw_task,
        }
    ]


@pytest.mark.asyncio
async def test_task_upsert_404_soft_deletes_matching_task(monkeypatch) -> None:
    """A missing provider task from an upsert event becomes a local tombstone."""
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
        def __init__(self, *, token_info: object, task_id: str) -> None:
            assert token_info is expected_token_info
            assert task_id == "456"

        async def fetch(self) -> None:
            return None

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.tasks.handler.iter_hubspot_integrations_for_portal",
        fake_targets,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.tasks.handler.HubSpotTaskRecordSyncService",
        FakeService,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.tasks.handler.HubSpotSingleTaskFetcher",
        FakeFetcher,
    )
    monkeypatch.setattr(HubSpotTaskWebhookHandler, "_soft_delete_task", soft_delete)
    event = HubSpotWebhookEvent(
        subscription_type=TASK_PROPERTY_CHANGE,
        portal_id="123",
        object_id="456",
        event_id="789",
        occurred_at=None,
        raw={},
    )

    await HubSpotTaskWebhookHandler.handle(event)

    soft_delete.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="456",
    )
