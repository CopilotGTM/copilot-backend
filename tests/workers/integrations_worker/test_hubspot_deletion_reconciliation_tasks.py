"""Regression coverage for HubSpot deletion reconciliation tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import UUID, uuid4

from src.workers.integrations_worker.tasks.crm.hubspot import (
    hubspot_deletion_reconciliation_all_orgs_task as all_orgs_module,
)
from src.workers.integrations_worker.tasks.crm.hubspot import (
    hubspot_deletion_reconciliation_task as reconciliation_module,
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


class _ServiceResult:
    checked_count = 9
    missing_count = 2
    soft_deleted_count = 2


def test_hubspot_deletion_reconciliation_task_runs_service_for_core_objects() -> None:
    """Regression test for hubspot deletion reconciliation task runs service for core objects."""

    organisation_id = uuid4()
    integration_id = uuid4()
    service = MagicMock()
    service.run = AsyncMock(return_value=_ServiceResult())

    with (
        patch.object(reconciliation_module, "run_async", side_effect=_run_coroutine),
        patch.object(
            reconciliation_module,
            "HubSpotDeletionReconciliationService",
            return_value=service,
        ),
    ):
        result = reconciliation_module.reconcile_hubspot_deletions_task.run(
            str(organisation_id),
            str(integration_id),
        )

    service.run.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_keys=("deals", "companies", "contacts", "tasks", "notes"),
    )
    assert result["status"] == "success"
    assert result["checked_count"] == 9
    assert result["missing_count"] == 2
    assert result["soft_deleted_count"] == 2


def test_hubspot_deletion_reconciliation_all_orgs_fans_out_ready_integrations_only() -> None:
    """Regression test for hubspot deletion reconciliation all orgs fans out ready integrations only."""

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

        yield org_sessions[organisation_id]

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
        result = all_orgs_module.reconcile_hubspot_deletions_all_orgs_task.run()

    send_task.assert_called_once_with(
        task_name="integrations.reconcile_hubspot_deletions",
        kwargs={
            "organisation_id": str(ready_org_id),
            "integration_id": str(ready_integration_id),
        },
    )
    assert result["status"] == "success"
    assert result["dispatched"] == 1
    assert result["skipped_not_ready"] == 1
