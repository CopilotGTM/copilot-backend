from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.workers.integrations_worker.tasks.crm.hubspot import (
    hubspot_deal_schema_task as hubspot_deal_schema_module,
)
from src.workers.integrations_worker.tasks.crm.salesforce import (
    salesforce_deal_record_sync_task as salesforce_deal_module,
)


def _run_coroutine(coro):
    return asyncio.run(coro)


async def _acquire_session(_organisation_id):
    yield AsyncMock()


def test_hubspot_deal_schema_task_records_runtime_failures_before_reraising() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    sync_id = uuid4()
    service = MagicMock()
    service.run = AsyncMock(side_effect=RuntimeError("hubspot boom"))

    with (
        patch.object(
            hubspot_deal_schema_module,
            "acquire_org_async_session",
            side_effect=_acquire_session,
        ),
        patch.object(
            hubspot_deal_schema_module.SyncHistoryManager,
            "start_fresh",
            new=AsyncMock(return_value=sync_id),
        ),
        patch.object(
            hubspot_deal_schema_module,
            "record_crm_task_failure",
            new=AsyncMock(),
        ) as record_task_failure,
        patch.object(
            hubspot_deal_schema_module,
            "run_async",
            side_effect=_run_coroutine,
        ),
        patch.object(
            hubspot_deal_schema_module,
            "HubSpotDealSchemaSyncService",
            return_value=service,
        ),
        pytest.raises(RuntimeError, match="hubspot boom"),
    ):
        hubspot_deal_schema_module.sync_hubspot_deal_schema_task.run(
            str(organisation_id),
            str(integration_id),
        )

    record_task_failure.assert_awaited_once()
    failure_kwargs = record_task_failure.await_args.kwargs
    assert failure_kwargs["organisation_id"] == organisation_id
    assert failure_kwargs["integration_id"] == integration_id
    assert failure_kwargs["sync_id"] == sync_id
    assert failure_kwargs["error_class"] == "RuntimeError"
    assert failure_kwargs["error_message"] == "hubspot boom"
    assert str(failure_kwargs["error"]) == "hubspot boom"
    assert failure_kwargs["mark_history_failed"] is hubspot_deal_schema_module._mark_history_failed
    assert (
        failure_kwargs["update_integration_metadata"]
        is hubspot_deal_schema_module._update_integration_metadata
    )


def test_salesforce_deal_task_records_runtime_failures_before_reraising() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    sync_id = uuid4()
    service = MagicMock()
    service.run = AsyncMock(side_effect=RuntimeError("salesforce boom"))

    with (
        patch.object(
            salesforce_deal_module,
            "acquire_org_async_session",
            side_effect=_acquire_session,
        ),
        patch.object(
            salesforce_deal_module.SyncHistoryManager,
            "start_fresh",
            new=AsyncMock(return_value=sync_id),
        ),
        patch.object(
            salesforce_deal_module,
            "record_crm_task_failure",
            new=AsyncMock(),
        ) as record_task_failure,
        patch.object(
            salesforce_deal_module,
            "run_async",
            side_effect=_run_coroutine,
        ),
        patch.object(
            salesforce_deal_module,
            "SalesforceDealRecordSyncService",
            return_value=service,
        ),
        pytest.raises(RuntimeError, match="salesforce boom"),
    ):
        salesforce_deal_module.sync_salesforce_deals_task.run(
            str(organisation_id),
            str(integration_id),
        )

    record_task_failure.assert_awaited_once()
    failure_kwargs = record_task_failure.await_args.kwargs
    assert failure_kwargs["organisation_id"] == organisation_id
    assert failure_kwargs["integration_id"] == integration_id
    assert failure_kwargs["sync_id"] == sync_id
    assert failure_kwargs["error_class"] == "RuntimeError"
    assert failure_kwargs["error_message"] == "salesforce boom"
    assert str(failure_kwargs["error"]) == "salesforce boom"
    assert failure_kwargs["mark_history_failed"] is salesforce_deal_module._mark_history_failed
    assert (
        failure_kwargs["update_integration_metadata"]
        is salesforce_deal_module._update_integration_metadata
    )
