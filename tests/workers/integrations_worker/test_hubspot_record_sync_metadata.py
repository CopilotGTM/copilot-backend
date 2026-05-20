"""Regression coverage for HubSpot record sync metadata cursors."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.workers.integrations_worker.tasks.crm.hubspot import (
    hubspot_company_record_sync_task,
    hubspot_contact_record_sync_task,
    hubspot_deal_record_sync_task,
    hubspot_note_record_sync_task,
    hubspot_task_record_sync_task,
)


class _ExecuteResult:
    def __init__(self, integration: SimpleNamespace) -> None:
        self.integration = integration

    def scalar_one_or_none(self) -> SimpleNamespace:
        return self.integration


class _FakeSession:
    def __init__(self, integration: SimpleNamespace) -> None:
        self.integration = integration
        self.added: list[SimpleNamespace] = []
        self.committed = False

    async def execute(self, _statement):  # noqa: ANN001
        return _ExecuteResult(self.integration)

    def add(self, integration: SimpleNamespace) -> None:
        self.added.append(integration)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.parametrize(
    ("module", "object_key"),
    [
        (hubspot_company_record_sync_task, "companies"),
        (hubspot_contact_record_sync_task, "contacts"),
        (hubspot_deal_record_sync_task, "deals"),
        (hubspot_note_record_sync_task, "notes"),
        (hubspot_task_record_sync_task, "tasks"),
    ],
)
@pytest.mark.asyncio
async def test_hubspot_record_sync_metadata_uses_task_start_time(
    module,
    object_key: str,
) -> None:
    """Full-sync cursors must start from task start time to avoid bootstrap gaps."""

    sync_started_at = "2026-05-21T10:00:00+00:00"
    integration = SimpleNamespace(
        connection_metadata={"record_sync": {}},
        last_synced_at=None,
    )
    session = _FakeSession(integration)

    await module._update_integration_metadata(
        session=session,
        organisation_id=uuid4(),
        integration_id=uuid4(),
        status="success",
        result_metadata={},
        error_class=None,
        error_message=None,
        sync_started_at=sync_started_at,
    )

    assert (
        integration.connection_metadata["record_sync"][object_key]["last_synced_at"]
        == sync_started_at
    )
    assert session.added == [integration]
    assert session.committed is True
