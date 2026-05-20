"""Regression coverage for HubSpot core incremental tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import UUID, uuid4

from src.service.integrations.crm.records.hubspot.incremental.registry import (
    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
)
from src.service.integrations.crm.records.hubspot.incremental_cursor_metadata import (
    is_hubspot_incremental_ready,
)
from src.workers.integrations_worker.tasks.crm.hubspot import (
    hubspot_core_incremental_all_orgs_task as all_orgs_module,
)
from src.workers.integrations_worker.tasks.crm.hubspot import (
    hubspot_core_incremental_task as incremental_module,
)


def _run_coroutine(coro):
    return asyncio.run(coro)


@dataclass(slots=True)
class _FakeScalarResult:
    rows: list[Any]

    def all(self) -> list[Any]:
        """Perform the database/session operation needed by this test harness."""

        return self.rows


@dataclass(slots=True)
class _FakeExecuteResult:
    rows: list[Any]

    def all(self) -> list[Any]:
        """Perform the database/session operation needed by this test harness."""

        return self.rows

    def scalars(self) -> _FakeScalarResult:
        """Perform the database/session operation needed by this test harness."""

        return _FakeScalarResult(self.rows)


class _FakeSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, _statement) -> _FakeExecuteResult:  # noqa: ANN001
        """Perform the database/session operation needed by this test harness."""

        return _FakeExecuteResult(self._rows)


async def _yield_session(session: _FakeSession):
    yield session


class _ServiceResult:
    seen_count = 3
    written_count = 2
    skipped_stale_count = 1


def test_hubspot_core_incremental_task_runs_service_for_core_objects() -> None:
    """Regression test for hubspot core incremental task runs service for core objects."""

    organisation_id = uuid4()
    integration_id = uuid4()
    service = MagicMock()
    service.run = AsyncMock(return_value=_ServiceResult())

    with (
        patch.object(incremental_module, "run_async", side_effect=_run_coroutine),
        patch.object(
            incremental_module,
            "HubSpotCoreIncrementalSyncService",
            return_value=service,
        ),
    ):
        result = incremental_module.sync_hubspot_core_incremental_task.run(
            str(organisation_id),
            str(integration_id),
        )

    service.run.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_keys=("deals", "companies", "contacts", "tasks", "notes"),
    )
    assert result["status"] == "success"
    assert result["seen_count"] == 3
    assert result["written_count"] == 2
    assert result["skipped_stale_count"] == 1


def test_hubspot_core_incremental_task_retries_transient_service_failures() -> None:
    """Regression test for hubspot core incremental task retries transient service failures."""

    organisation_id = uuid4()
    integration_id = uuid4()
    failure = RuntimeError("hubspot unavailable")
    service = MagicMock()
    service.run = AsyncMock(side_effect=failure)
    retry = Mock(side_effect=RuntimeError("retry requested"))

    with (
        patch.object(incremental_module, "run_async", side_effect=_run_coroutine),
        patch.object(
            incremental_module,
            "HubSpotCoreIncrementalSyncService",
            return_value=service,
        ),
        patch.object(incremental_module.sync_hubspot_core_incremental_task, "retry", retry),
    ):
        incremental_module.sync_hubspot_core_incremental_task.request.retries = 0
        try:
            incremental_module.sync_hubspot_core_incremental_task.run(
                str(organisation_id),
                str(integration_id),
            )
        except RuntimeError as exc:
            assert str(exc) == "retry requested"

    retry.assert_called_once()
    assert retry.call_args.kwargs["exc"] is failure


def test_hubspot_core_incremental_readiness_accepts_existing_full_sync_metadata() -> None:
    """Regression test for hubspot core incremental readiness accepts existing full sync metadata."""

    metadata = {
        "record_sync": {
            "deals": {"last_synced_at": "2026-05-19T10:00:00+00:00"},
            "companies": {"last_synced_at": "2026-05-19T10:00:00+00:00"},
            "contacts": {"last_synced_at": "2026-05-19T10:00:00+00:00"},
            "tasks": {"last_synced_at": "2026-05-19T10:00:00+00:00"},
            "notes": {"last_synced_at": "2026-05-19T10:00:00+00:00"},
        }
    }

    assert is_hubspot_incremental_ready(metadata, HUBSPOT_INCREMENTAL_OBJECT_KEYS) is True


def test_hubspot_core_incremental_all_orgs_fans_out_ready_integrations_only() -> None:
    """Regression test for hubspot core incremental all orgs fans out ready integrations only."""

    ready_org_id = uuid4()
    not_ready_org_id = uuid4()
    ready_integration_id = uuid4()
    not_ready_integration_id = uuid4()

    ready_integration = MagicMock()
    ready_integration.organisation_id = ready_org_id
    ready_integration.integration_id = ready_integration_id
    ready_integration.is_active = True
    ready_integration.connection_metadata = {"initial_sync_status": "success"}

    not_ready_integration = MagicMock()
    not_ready_integration.organisation_id = not_ready_org_id
    not_ready_integration.integration_id = not_ready_integration_id
    not_ready_integration.is_active = True
    not_ready_integration.connection_metadata = {"initial_sync_status": "queued"}

    org_sessions = {
        ready_org_id: _FakeSession([ready_integration]),
        not_ready_org_id: _FakeSession([not_ready_integration]),
    }

    async def acquire_master_session():
        """Perform the database/session operation needed by this test harness."""

        yield _FakeSession([(ready_org_id,), (not_ready_org_id,)])

    async def acquire_org_session(organisation_id: UUID):
        """Perform the database/session operation needed by this test harness."""

        async for session in _yield_session(org_sessions[organisation_id]):
            yield session

    send_task = Mock(return_value="task-123")

    with (
        patch.object(all_orgs_module, "run_async", side_effect=_run_coroutine),
        patch.object(
            all_orgs_module,
            "acquire_master_async_session",
            side_effect=acquire_master_session,
        ),
        patch.object(
            all_orgs_module,
            "acquire_org_async_session",
            side_effect=acquire_org_session,
        ),
        patch.object(all_orgs_module, "send_integrations_task", send_task),
    ):
        result = all_orgs_module.sync_hubspot_core_incremental_all_orgs_task.run()

    send_task.assert_called_once_with(
        task_name="integrations.sync_hubspot_core_incremental",
        kwargs={
            "organisation_id": str(ready_org_id),
            "integration_id": str(ready_integration_id),
        },
    )
    assert result["status"] == "success"
    assert result["orgs_scanned"] == 2
    assert result["dispatched"] == 1
    assert result["skipped_not_ready"] == 1
    assert datetime.fromisoformat(result["completed_at"]).tzinfo == UTC
