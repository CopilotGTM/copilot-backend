"""Incremental refresh support for HubSpot tasks."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
    require_token_org_context,
)
from src.service.integrations.crm.records.hubspot.tasks.task_sync_service import (
    HubSpotTaskRecordSyncService,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_task_fetcher import (
    HubSpotSingleTaskFetcher,
)


class HubSpotTaskIncrementalRefresher:
    """Refresh one HubSpot task activity from a full provider snapshot."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_task = await HubSpotSingleTaskFetcher(
            token_info=token_info,
            task_id=changed_object.object_id,
        ).fetch()
        if raw_task is None:
            return HubSpotIncrementalRefreshResult()

        organisation_id, integration_id = require_token_org_context(token_info)
        write_result = await HubSpotTaskRecordSyncService().persist_raw_task(
            organisation_id=organisation_id,
            integration_id=integration_id,
            raw_task=raw_task,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.tasks_created
                + write_result.tasks_updated
                + write_result.tasks_reactivated
            ),
            skipped_stale_count=write_result.tasks_skipped_stale,
        )
