"""Incremental refresh support for HubSpot notes."""

from __future__ import annotations

from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalRefreshResult,
    require_token_org_context,
)
from src.service.integrations.crm.records.hubspot.notes.note_sync_service import (
    HubSpotNoteRecordSyncService,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)
from src.workers.integrations_worker.providers.crm.hubspot.single_note_fetcher import (
    HubSpotSingleNoteFetcher,
)


class HubSpotNoteIncrementalRefresher:
    """Refresh one HubSpot note activity from a full provider snapshot."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        raw_note = await HubSpotSingleNoteFetcher(
            token_info=token_info,
            note_id=changed_object.object_id,
        ).fetch()
        if raw_note is None:
            return HubSpotIncrementalRefreshResult()

        organisation_id, integration_id = require_token_org_context(token_info)
        write_result = await HubSpotNoteRecordSyncService().persist_raw_note(
            organisation_id=organisation_id,
            integration_id=integration_id,
            raw_note=raw_note,
        )
        return HubSpotIncrementalRefreshResult(
            written_count=(
                write_result.notes_created
                + write_result.notes_updated
                + write_result.notes_reactivated
            ),
            skipped_stale_count=write_result.notes_skipped_stale,
        )
