from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from src.workers.integrations_worker.tasks.crm.failure_handling import (
    record_crm_task_failure,
)


async def _acquire_session(_organisation_id):
    yield "session"


@pytest.mark.asyncio
async def test_record_crm_task_failure_persists_history_metadata_and_health() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    sync_id = uuid4()
    error = RuntimeError("boom")
    mark_history_failed = AsyncMock()
    update_metadata = AsyncMock()

    with (
        patch(
            "src.workers.integrations_worker.tasks.crm.failure_handling.acquire_org_async_session",
            side_effect=_acquire_session,
        ),
        patch(
            "src.workers.integrations_worker.tasks.crm.failure_handling.record_sync_failure",
            new=AsyncMock(),
        ) as record_sync_failure,
    ):
        await record_crm_task_failure(
            organisation_id=organisation_id,
            integration_id=integration_id,
            sync_id=sync_id,
            error_class="RuntimeError",
            error_message="boom",
            error=error,
            mark_history_failed=mark_history_failed,
            update_integration_metadata=update_metadata,
        )

    mark_history_failed.assert_awaited_once_with(
        organisation_id,
        sync_id,
        error_class="RuntimeError",
        error_message="boom",
    )
    update_metadata.assert_awaited_once_with(
        session="session",
        organisation_id=organisation_id,
        integration_id=integration_id,
        status="failed",
        result_metadata={},
        error_class="RuntimeError",
        error_message="boom",
    )
    record_sync_failure.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=integration_id,
        error=error,
    )
