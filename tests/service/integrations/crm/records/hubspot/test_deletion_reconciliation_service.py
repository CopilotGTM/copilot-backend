"""Regression coverage for HubSpot deletion reconciliation service."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.integrations.crm.records.hubspot.deletion_reconciliation_service import (
    HubSpotDeletionReconciliationService,
)


@pytest.mark.asyncio
async def test_reconciliation_soft_deletes_only_hubspot_missing_ids(monkeypatch) -> None:
    """Regression test for reconciliation soft deletes only hubspot missing ids."""

    token_info = SimpleNamespace(organisation_id=uuid4(), integration_id=uuid4())
    deleted_batches: list[list[str]] = []

    class FakeBatchFetcher:
        """Test fetcher that reports which external ids currently exist in HubSpot."""

        def __init__(self, *, token_info, object_type, object_ids):  # noqa: ANN001
            assert object_type == "deals"
            self.object_ids = object_ids

        async def fetch_existing_ids(self) -> set[str]:
            """Execute fetch existing ids."""

            return {"101", "103"}

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.HubSpotBatchObjectExistenceFetcher",
        FakeBatchFetcher,
    )

    async def fake_soft_delete(self, *, organisation_id, integration_id, object_key, external_ids):  # noqa: ANN001
        """Execute fake soft delete."""

        assert object_key == "deals"
        deleted_batches.append(list(external_ids))
        return len(external_ids)

    monkeypatch.setattr(
        HubSpotDeletionReconciliationService,
        "_soft_delete_external_ids",
        fake_soft_delete,
    )

    service = HubSpotDeletionReconciliationService(batch_size=100)
    service._load_active_external_ids = AsyncMock(return_value=["101", "102", "103"])  # type: ignore[method-assign]

    result = await service.reconcile_object(
        object_key="deals",
        token_info=token_info,
        checked_at=datetime(2026, 5, 19, 10, tzinfo=UTC),
    )

    assert deleted_batches == [["102"]]
    assert result.checked_count == 3
    assert result.missing_count == 1
    assert result.soft_deleted_count == 1


@pytest.mark.asyncio
async def test_reconciliation_chunks_hubspot_batch_reads(monkeypatch) -> None:
    """Regression test for reconciliation chunks hubspot batch reads."""

    token_info = SimpleNamespace(organisation_id=uuid4(), integration_id=uuid4())
    requested_batches: list[list[str]] = []

    class FakeBatchFetcher:
        """Test fetcher that reports which external ids currently exist in HubSpot."""

        def __init__(self, *, token_info, object_type, object_ids):  # noqa: ANN001
            requested_batches.append(list(object_ids))
            self.object_ids = object_ids

        async def fetch_existing_ids(self) -> set[str]:
            """Execute fetch existing ids."""

            return set(self.object_ids)

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.HubSpotBatchObjectExistenceFetcher",
        FakeBatchFetcher,
    )

    service = HubSpotDeletionReconciliationService(batch_size=2)
    service._load_active_external_ids = AsyncMock(return_value=["1", "2", "3"])  # type: ignore[method-assign]
    service._soft_delete_external_ids = AsyncMock(return_value=0)  # type: ignore[method-assign]

    result = await service.reconcile_object(
        object_key="contacts",
        token_info=token_info,
        checked_at=datetime(2026, 5, 19, 10, tzinfo=UTC),
    )

    assert requested_batches == [["1", "2"], ["3"]]
    assert result.soft_deleted_count == 0


@pytest.mark.asyncio
async def test_reconciliation_accepts_task_and_note_objects(monkeypatch) -> None:
    """Regression test for task/note delete reconciliation object support."""

    token_info = SimpleNamespace(organisation_id=uuid4(), integration_id=uuid4())
    object_types: list[str] = []

    class FakeBatchFetcher:
        """Test fetcher records the HubSpot object type passed to batch-read."""

        def __init__(self, *, token_info, object_type, object_ids):  # noqa: ANN001
            object_types.append(object_type)
            self.object_ids = object_ids

        async def fetch_existing_ids(self) -> set[str]:
            """Execute fetch existing ids."""

            return set(self.object_ids)

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.HubSpotBatchObjectExistenceFetcher",
        FakeBatchFetcher,
    )

    service = HubSpotDeletionReconciliationService(batch_size=100)
    service._load_active_external_ids = AsyncMock(return_value=["task-1"])  # type: ignore[method-assign]
    service._soft_delete_external_ids = AsyncMock(return_value=0)  # type: ignore[method-assign]

    for object_key in ("tasks", "notes"):
        result = await service.reconcile_object(
            object_key=object_key,
            token_info=token_info,
            checked_at=datetime(2026, 5, 19, 10, tzinfo=UTC),
        )
        assert result.checked_count == 1
        assert result.soft_deleted_count == 0

    assert object_types == ["tasks", "notes"]


@pytest.mark.asyncio
async def test_reconciliation_updates_metadata_for_each_object(monkeypatch) -> None:
    """Regression test for reconciliation updates metadata for each object."""

    organisation_id = uuid4()
    integration_id = uuid4()
    metadata = {"record_sync": {"deals": {"status": "success"}}}
    token_info = SimpleNamespace(organisation_id=organisation_id, integration_id=integration_id)

    async def fake_reconcile_object(self, *, object_key, token_info, checked_at):  # noqa: ANN001
        """Execute fake reconcile object."""

        return SimpleNamespace(
            object_key=object_key,
            checked_count=2,
            missing_count=1,
            soft_deleted_count=1,
        )

    service = HubSpotDeletionReconciliationService()
    service._load_token_context = AsyncMock(return_value=(token_info, metadata))  # type: ignore[method-assign]
    service._save_metadata = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        HubSpotDeletionReconciliationService,
        "reconcile_object",
        fake_reconcile_object,
    )

    result = await service.run(
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_keys=("deals", "companies"),
        checked_at=datetime(2026, 5, 19, 10, tzinfo=UTC),
    )

    saved_metadata = service._save_metadata.await_args.kwargs["metadata"]  # type: ignore[attr-defined]
    assert result.soft_deleted_count == 2
    assert saved_metadata["record_sync"]["deals"]["delete_reconciliation"] == {
        "last_checked_at": "2026-05-19T10:00:00+00:00",
        "last_checked_count": 2,
        "last_missing_count": 1,
        "last_soft_deleted_count": 1,
    }
    assert (
        saved_metadata["record_sync"]["companies"]["delete_reconciliation"][
            "last_soft_deleted_count"
        ]
        == 1
    )
