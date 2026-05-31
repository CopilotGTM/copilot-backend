from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from src.service.integrations.crm.records.salesforce.users.user_types import UserSyncResult
from src.workers.integrations_worker.tasks.crm.salesforce import salesforce_user_record_sync_task


def _run_coroutine(coro):
    return asyncio.run(coro)


async def _acquire_session(_organisation_id):
    yield AsyncMock()


def test_salesforce_user_sync_restarts_requested_cdc_channel(monkeypatch) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    queued: list[dict[str, object]] = []

    async def fake_start_fresh(_session, _integration_id):
        return uuid4()

    async def fake_complete_fresh(*_args, **_kwargs):
        return None

    class FakeUserService:
        async def run(self, *, organisation_id, integration_id):
            return UserSyncResult(
                provider="salesforce",
                object_type="user",
                organisation_id=organisation_id,
                integration_id=integration_id,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )

    async def fake_update_metadata(**_kwargs):
        return None

    async def fake_send_task(**kwargs):
        queued.append(kwargs)
        return "task-id"

    monkeypatch.setattr(salesforce_user_record_sync_task, "run_async", _run_coroutine)
    monkeypatch.setattr(
        salesforce_user_record_sync_task,
        "acquire_org_async_session",
        _acquire_session,
    )
    monkeypatch.setattr(
        salesforce_user_record_sync_task.SyncHistoryManager,
        "start_fresh",
        fake_start_fresh,
    )
    monkeypatch.setattr(
        salesforce_user_record_sync_task.SyncHistoryManager,
        "complete_fresh",
        fake_complete_fresh,
    )
    monkeypatch.setattr(
        salesforce_user_record_sync_task,
        "SalesforceUserRecordSyncService",
        FakeUserService,
    )
    monkeypatch.setattr(
        salesforce_user_record_sync_task,
        "_update_integration_metadata",
        fake_update_metadata,
    )
    monkeypatch.setattr(
        salesforce_user_record_sync_task,
        "send_integrations_task_async",
        fake_send_task,
    )

    result = salesforce_user_record_sync_task.sync_salesforce_users_task.run(
        str(org_id),
        str(integration_id),
        start_cdc_after_success=True,
        cdc_channel="/data/UserChangeEvent",
    )

    assert result["salesforce_cdc_task_id"] == "task-id"
    assert queued == [
        {
            "task_name": "integrations.run_salesforce_cdc_subscriber",
            "kwargs": {
                "organisation_id": str(org_id),
                "integration_id": str(integration_id),
                "channel": "/data/UserChangeEvent",
            },
            "queue": "integrations_streaming",
            "routing_key": "integrations_streaming",
        }
    ]
