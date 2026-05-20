"""Tests for HubSpot core incremental sync service."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.integrations.crm.records.hubspot.core_incremental_sync_service import (
    HubSpotCoreIncrementalSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
)
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)


def _token_info(*, organisation_id=None, integration_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        organisation_id=organisation_id or uuid4(),
        integration_id=integration_id or uuid4(),
    )


class _RecordingRefresher:
    """Test refresher that records which changed objects the core service dispatches."""

    def __init__(self, *, stale_object_ids: set[str] | None = None) -> None:
        self.refreshed_object_ids: list[str] = []
        self.stale_object_ids = stale_object_ids or set()

    async def refresh(self, *, token_info, changed_object):  # noqa: ANN001
        """Record dispatch and return deterministic write/stale counters."""
        self.refreshed_object_ids.append(changed_object.object_id)
        if changed_object.object_id in self.stale_object_ids:
            return HubSpotIncrementalRefreshResult(skipped_stale_count=1)
        return HubSpotIncrementalRefreshResult(written_count=1)


@pytest.mark.asyncio
async def test_incremental_run_skips_when_initial_sync_is_not_ready(monkeypatch) -> None:
    """Regression test for child tasks not polling HubSpot during initial sync."""

    metadata = {
        "initial_sync_status": "running",
        "record_sync": {
            "deals": {"last_synced_at": "2026-05-21T10:00:00+00:00"},
        },
    }
    token_info = _token_info()

    async def fake_load_token_context(self, *, organisation_id, integration_id):  # noqa: ANN001
        """Return partial initial-sync metadata like a queued task can observe."""
        return token_info, metadata

    async def fail_fetch_changed_hubspot_objects(**kwargs):  # noqa: ANN003
        """Fail if the service polls HubSpot before initial sync finishes."""
        raise AssertionError("HubSpot Search should not run before initial sync is ready")

    async def fail_save_metadata(self, *, organisation_id, integration_id, metadata):  # noqa: ANN001
        """Fail if a skipped readiness check writes misleading cursor metadata."""
        raise AssertionError("Skipped incremental sync should not write metadata")

    monkeypatch.setattr(
        HubSpotCoreIncrementalSyncService,
        "_load_token_context",
        fake_load_token_context,
    )
    monkeypatch.setattr(
        HubSpotCoreIncrementalSyncService,
        "_save_metadata",
        fail_save_metadata,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.core_incremental_sync_service.fetch_changed_hubspot_objects",
        fail_fetch_changed_hubspot_objects,
    )

    result = await HubSpotCoreIncrementalSyncService().run(
        organisation_id=token_info.organisation_id,
        integration_id=token_info.integration_id,
        object_keys=("deals",),
    )

    assert result.object_results == ()
    assert result.updated_metadata == metadata


@pytest.mark.asyncio
async def test_incremental_deals_searches_from_cursor_and_updates_metadata(monkeypatch) -> None:
    """Regression test for incremental deals searches from cursor and updates metadata."""

    token_info = _token_info()
    metadata = {
        "record_sync": {
            "deals": {
                "last_synced_at": "2026-05-18T09:00:00+00:00",
                "incremental": {
                    "cursor_source_modified_at": "2026-05-18T10:00:00+00:00",
                    "lookback_minutes": 10,
                },
            }
        }
    }
    searches: list[datetime] = []

    async def fake_fetch_changed_hubspot_objects(
        *,
        token_info,  # noqa: ANN001
        object_type: str,
        modified_since: datetime,
    ) -> list[ChangedHubSpotObject]:
        """Return two changed deals so the test can verify cursor maxing."""
        assert object_type == "deals"
        searches.append(modified_since)
        return [
            ChangedHubSpotObject(
                object_id="101",
                source_modified_at=datetime(2026, 5, 18, 10, 5, tzinfo=UTC),
                raw={},
            ),
            ChangedHubSpotObject(
                object_id="102",
                source_modified_at=datetime(2026, 5, 18, 10, 7, tzinfo=UTC),
                raw={},
            ),
        ]

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.core_incremental_sync_service.fetch_changed_hubspot_objects",
        fake_fetch_changed_hubspot_objects,
    )

    refresher = _RecordingRefresher(stale_object_ids={"102"})
    result = await HubSpotCoreIncrementalSyncService().sync_object(
        object_key="deals",
        token_info=token_info,
        metadata=metadata,
        refresher_registry={"deals": refresher},
        cursor_updated_at=datetime(2026, 5, 18, 10, 10, tzinfo=UTC),
    )

    assert searches == [datetime(2026, 5, 18, 9, 50, tzinfo=UTC)]
    assert refresher.refreshed_object_ids == ["101", "102"]
    assert result.seen_count == 2
    assert result.written_count == 1
    assert result.skipped_stale_count == 1
    incremental = result.updated_metadata["record_sync"]["deals"]["incremental"]
    assert incremental["cursor_source_modified_at"] == "2026-05-18T10:07:00+00:00"
    assert incremental["cursor_updated_at"] == "2026-05-18T10:10:00+00:00"
    assert incremental["last_seen_count"] == 2
    assert incremental["last_written_count"] == 1
    assert incremental["last_skipped_stale_count"] == 1


@pytest.mark.asyncio
async def test_incremental_companies_uses_last_full_sync_when_cursor_missing(monkeypatch) -> None:
    """Regression test for incremental companies uses last full sync when cursor missing."""

    token_info = _token_info()
    metadata = {
        "record_sync": {
            "companies": {
                "last_synced_at": "2026-05-18T10:00:00+00:00",
            }
        }
    }
    searches: list[datetime] = []

    async def fake_fetch_changed_hubspot_objects(
        *,
        token_info,  # noqa: ANN001
        object_type: str,
        modified_since: datetime,
    ) -> list[ChangedHubSpotObject]:
        """Return no rows to prove empty searches preserve provider cursor state."""
        assert object_type == "companies"
        searches.append(modified_since)
        return []

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.core_incremental_sync_service.fetch_changed_hubspot_objects",
        fake_fetch_changed_hubspot_objects,
    )

    result = await HubSpotCoreIncrementalSyncService().sync_object(
        object_key="companies",
        token_info=token_info,
        metadata=metadata,
        refresher_registry={"companies": _RecordingRefresher()},
        cursor_updated_at=datetime(2026, 5, 18, 10, 5, tzinfo=UTC),
    )

    assert searches == [datetime(2026, 5, 18, 9, 50, tzinfo=UTC)]
    incremental = result.updated_metadata["record_sync"]["companies"]["incremental"]
    assert (
        "cursor_source_modified_at" not in incremental
        or incremental["cursor_source_modified_at"] is None
    )
    assert incremental["cursor_updated_at"] == "2026-05-18T10:05:00+00:00"
    assert incremental["last_seen_count"] == 0


@pytest.mark.parametrize("object_key", ["companies", "contacts", "tasks", "notes"])
@pytest.mark.asyncio
async def test_incremental_dispatches_changed_objects_to_registered_refresher(
    monkeypatch,
    object_key: str,
) -> None:
    """Regression test for routing each object family through its registered refresher."""

    token_info = _token_info()
    metadata = {
        "record_sync": {
            object_key: {
                "incremental": {
                    "cursor_source_modified_at": "2026-05-18T10:00:00+00:00",
                    "lookback_minutes": 10,
                }
            }
        }
    }

    async def fake_fetch_changed_hubspot_objects(
        *,
        token_info,  # noqa: ANN001
        object_type: str,
        modified_since: datetime,
    ) -> list[ChangedHubSpotObject]:
        """Return one changed row for the object family under test."""
        assert object_type == object_key
        return [
            ChangedHubSpotObject(
                object_id=f"{object_key}-1",
                source_modified_at=datetime(2026, 5, 18, 10, 4, tzinfo=UTC),
                raw={},
            )
        ]

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.core_incremental_sync_service.fetch_changed_hubspot_objects",
        fake_fetch_changed_hubspot_objects,
    )

    refresher = _RecordingRefresher()
    result = await HubSpotCoreIncrementalSyncService().sync_object(
        object_key=object_key,
        token_info=token_info,
        metadata=metadata,
        refresher_registry={object_key: refresher},
        cursor_updated_at=datetime(2026, 5, 18, 10, 10, tzinfo=UTC),
    )

    assert refresher.refreshed_object_ids == [f"{object_key}-1"]
    assert result.written_count == 1
    assert result.skipped_stale_count == 0


@pytest.mark.asyncio
async def test_incremental_sync_rejects_missing_start_cursor() -> None:
    """Regression test for incremental sync rejects missing start cursor."""

    with pytest.raises(ValueError, match="No HubSpot incremental start cursor"):
        await HubSpotCoreIncrementalSyncService().sync_object(
            object_key="deals",
            token_info=_token_info(),
            metadata={},
            refresher_registry={"deals": _RecordingRefresher()},
        )
