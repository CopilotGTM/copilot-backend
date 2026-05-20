"""Incremental refresh support for HubSpot contacts."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.contacts.contact_sync_service import (
    HubSpotContactRecordSyncService,
)
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
    require_token_org_context,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_contact_fetcher import (
    HubSpotSingleContactFetcher,
)


class HubSpotContactIncrementalRefresher:
    """Refresh one contact through the single-record contact writer."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_contact = await HubSpotSingleContactFetcher(
            token_info=token_info,
            contact_id=changed_object.object_id,
        ).fetch()
        if raw_contact is None:
            return HubSpotIncrementalRefreshResult()

        organisation_id, integration_id = require_token_org_context(token_info)
        write_result, _ = await HubSpotContactRecordSyncService().persist_raw_contact(
            organisation_id=organisation_id,
            integration_id=integration_id,
            raw_contact=raw_contact,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.contacts_created
                + write_result.contacts_updated
                + write_result.contacts_reactivated
            ),
            skipped_stale_count=write_result.contacts_skipped_stale,
        )
