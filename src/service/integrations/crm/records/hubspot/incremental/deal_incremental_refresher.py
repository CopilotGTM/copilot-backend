"""Incremental refresh support for HubSpot deals."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.deals.deal_sync_service import (
    HubSpotDealRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
    require_token_org_context,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_deal_fetcher import (
    HubSpotSingleDealFetcher,
)


class HubSpotDealIncrementalRefresher:
    """Refresh one deal through the same full-snapshot writer used by webhooks."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        deal_service = HubSpotDealRecordSyncService()
        property_list = await deal_service._build_property_list(token_info=token_info)
        raw_deal = await HubSpotSingleDealFetcher(
            token_info=token_info,
            deal_id=changed_object.object_id,
            properties=property_list,
        ).fetch()
        if raw_deal is None:
            return HubSpotIncrementalRefreshResult()

        organisation_id, integration_id = require_token_org_context(token_info)
        result = await deal_service._persist_raw_deals(
            organisation_id=organisation_id,
            integration_id=integration_id,
            raw_deals=[raw_deal],
            property_list_size=len(property_list),
        )
        return HubSpotIncrementalRefreshResult(
            written_count=result.deals_created + result.deals_updated,
            skipped_stale_count=result.deals_skipped_stale,
        )
