"""Regression coverage for HubSpot deletion reconciliation service."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import CRMNote, CRMSourceObjectType, CRMTask
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
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


@pytest.mark.asyncio
async def test_load_active_external_ids_uses_source_mappings_and_live_local_rows(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    live_deal_id = uuid4()
    stale_deal_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def load_local_id_map(self, *, object_type, **_kwargs):  # noqa: ANN001
            calls.append({"object_type": object_type})
            return {"101": live_deal_id, "102": stale_deal_id}

    class FakeResult:
        def all(self):
            return [(live_deal_id,)]

    class FakeSession:
        def __init__(self) -> None:
            self.statement = None

        async def execute(self, statement):  # noqa: ANN001
            self.statement = statement
            return FakeResult()

    session = FakeSession()

    async def acquire_session(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.CRMSourceMappingHelper",
        FakeMappingHelper,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.acquire_org_async_session",
        acquire_session,
    )

    result = await HubSpotDeletionReconciliationService()._load_active_external_ids(
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_key="deals",
    )

    where_clause = str(session.statement).partition("WHERE")[2]
    assert result == ["101"]
    assert calls == [{"object_type": CRMSourceObjectType.DEAL}]
    assert "crm_deals.deal_id" in where_clause
    assert ".external_id" not in where_clause


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_key", "object_type", "local_row_id", "expected_column"),
    [
        ("tasks", CRMSourceObjectType.TASK, "task_id", "crm_tasks.task_id"),
        ("notes", CRMSourceObjectType.NOTE, "note_id", "crm_notes.note_id"),
    ],
)
async def test_load_active_external_ids_uses_task_note_source_tables(
    monkeypatch,
    object_key: str,
    object_type: CRMSourceObjectType,
    local_row_id: str,
    expected_column: str,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    live_id = uuid4()
    stale_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def load_local_id_map(self, *, object_type, **_kwargs):  # noqa: ANN001
            calls.append({"object_type": object_type})
            return {"101": live_id, "102": stale_id}

    class FakeResult:
        def all(self):
            return [(live_id,)]

    class FakeSession:
        def __init__(self) -> None:
            self.statement = None

        async def execute(self, statement):  # noqa: ANN001
            self.statement = statement
            return FakeResult()

    session = FakeSession()

    async def acquire_session(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.CRMSourceMappingHelper",
        FakeMappingHelper,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.acquire_org_async_session",
        acquire_session,
    )

    result = await HubSpotDeletionReconciliationService()._load_active_external_ids(
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_key=object_key,
    )

    sql_text = str(session.statement)
    where_clause = sql_text.partition("WHERE")[2]
    assert result == ["101"]
    assert calls == [{"object_type": object_type}]
    assert expected_column in where_clause
    assert "crm_activities" not in sql_text
    assert local_row_id in expected_column


@pytest.mark.asyncio
async def test_soft_delete_external_ids_marks_mappings_then_deletes_local_rows(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    deal_id = uuid4()
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=deal_id,
        external_id="101",
    )
    calls: list[dict[str, object]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def mark_external_ids_deleted(self, *, object_type, external_ids):  # noqa: ANN001
            calls.append({"object_type": object_type, "external_ids": list(external_ids)})
            return {"101": deal_id}

    class FakeScalars:
        def all(self):
            return [deal]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        def __init__(self) -> None:
            self.statement = None
            self.committed = False

        async def execute(self, statement):  # noqa: ANN001
            self.statement = statement
            return FakeResult()

        def add(self, _row) -> None:  # noqa: ANN001
            return None

        async def commit(self) -> None:
            self.committed = True

    session = FakeSession()

    async def acquire_session(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.CRMSourceMappingHelper",
        FakeMappingHelper,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.acquire_org_async_session",
        acquire_session,
    )

    deleted = await HubSpotDeletionReconciliationService()._soft_delete_external_ids(
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_key="deals",
        external_ids=["101"],
    )

    where_clause = str(session.statement).partition("WHERE")[2]
    assert deleted == 1
    assert calls == [{"object_type": CRMSourceObjectType.DEAL, "external_ids": ["101"]}]
    assert "crm_deals.deal_id" in where_clause
    assert ".external_id" not in where_clause
    assert deal.is_deleted is True
    assert session.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_key", "object_type", "row_factory", "expected_column"),
    [
        (
            "tasks",
            CRMSourceObjectType.TASK,
            lambda org_id, int_id, local_id: CRMTask(
                organisation_id=org_id,
                integration_id=int_id,
                task_id=local_id,
                title="Provider task",
            ),
            "crm_tasks.task_id",
        ),
        (
            "notes",
            CRMSourceObjectType.NOTE,
            lambda org_id, int_id, local_id: CRMNote(
                organisation_id=org_id,
                integration_id=int_id,
                note_id=local_id,
                body="Provider note",
            ),
            "crm_notes.note_id",
        ),
    ],
)
async def test_soft_delete_external_ids_deletes_task_note_source_tables_and_projection(
    monkeypatch,
    object_key: str,
    object_type: CRMSourceObjectType,
    row_factory,
    expected_column: str,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    local_id = uuid4()
    row = row_factory(organisation_id, integration_id, local_id)
    calls: list[dict[str, object]] = []
    projection_deletes: list[object] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def mark_external_ids_deleted(self, *, object_type, external_ids):  # noqa: ANN001
            calls.append({"object_type": object_type, "external_ids": list(external_ids)})
            return {"101": local_id}

    class FakeProjectionService:
        def __init__(self, _session, _organisation_id):  # noqa: ANN001
            pass

        async def soft_delete_activity_projection(self, activity_id):  # noqa: ANN001
            projection_deletes.append(activity_id)

    class FakeScalars:
        def all(self):
            return [row]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        def __init__(self) -> None:
            self.statement = None
            self.committed = False

        async def execute(self, statement):  # noqa: ANN001
            self.statement = statement
            return FakeResult()

        def add(self, _row) -> None:  # noqa: ANN001
            return None

        async def commit(self) -> None:
            self.committed = True

    session = FakeSession()

    async def acquire_session(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.CRMSourceMappingHelper",
        FakeMappingHelper,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.ActivityFeedProjectionService",
        FakeProjectionService,
    )
    monkeypatch.setattr(
        "src.service.integrations.crm.records.hubspot.deletion_reconciliation_service.acquire_org_async_session",
        acquire_session,
    )

    deleted = await HubSpotDeletionReconciliationService()._soft_delete_external_ids(
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_key=object_key,
        external_ids=["101"],
    )

    sql_text = str(session.statement)
    where_clause = sql_text.partition("WHERE")[2]
    assert deleted == 1
    assert calls == [{"object_type": object_type, "external_ids": ["101"]}]
    assert expected_column in where_clause
    assert "crm_activities" not in sql_text
    assert row.is_deleted is True
    assert projection_deletes == [local_id]
    assert session.committed is True
