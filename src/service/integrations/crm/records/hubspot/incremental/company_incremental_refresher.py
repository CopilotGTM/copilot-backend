"""Incremental refresh support for HubSpot companies."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.companies.company_sync_service import (
    HubSpotCompanyRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
    require_token_org_context,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_company_fetcher import (
    HubSpotSingleCompanyFetcher,
)


class HubSpotCompanyIncrementalRefresher:
    """Refresh one company without triggering full-sync deactivation behavior."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_company = await HubSpotSingleCompanyFetcher(
            token_info=token_info,
            company_id=changed_object.object_id,
        ).fetch()
        if raw_company is None:
            return HubSpotIncrementalRefreshResult()

        organisation_id, integration_id = require_token_org_context(token_info)
        write_result, _ = await HubSpotCompanyRecordSyncService().persist_raw_company(
            organisation_id=organisation_id,
            integration_id=integration_id,
            raw_company=raw_company,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.companies_created
                + write_result.companies_updated
                + write_result.companies_reactivated
            ),
            skipped_stale_count=write_result.companies_skipped_stale,
        )
