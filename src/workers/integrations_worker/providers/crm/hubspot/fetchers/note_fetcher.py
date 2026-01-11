"""HubSpot Note data fetcher.

Fetches HubSpot CRM notes. Notes are later normalized into our canonical
CRM activity model (crm_activities) as activity_type=NOTE.

We intentionally keep this fetcher focused on retrieval; any cross-provider
normalization happens in the provider wrapper / sync services.
"""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.core import BaseFetcher

logger = get_logger(__name__)

HUBSPOT_NOTE_PROPERTIES = [
    # Core note content
    "hs_note_body",
    # Ownership
    "hubspot_owner_id",
    "hubspot_owner_email",
    "hubspot_owner_name",
    # Timestamps
    "hs_timestamp",
    "hs_lastmodifieddate",
    "hs_createdate",
    # Required by HubSpot objects API
    "hs_object_id",
]

# Associations to include with notes
HUBSPOT_NOTE_ASSOCIATIONS = ["deals", "contacts", "companies"]


class HubSpotNoteFetcher(BaseFetcher):
    """Fetcher for HubSpot note data."""

    async def fetch_all(
        self,
        *,
        limit: int = 100,
        incremental: bool = False,
        last_sync_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all notes from HubSpot.

        Args:
            limit: Number of records per page
            incremental: Whether to fetch only updated records
            last_sync_time: Unix timestamp in milliseconds for incremental sync

        Returns:
            List of note records with properties and associations
        """
        if incremental and last_sync_time:
            # Use search endpoint for incremental sync (requires POST)
            return await self._fetch_incremental(
                last_sync_time=last_sync_time,
                limit=limit,
            )

        # Full sync uses the standard GET endpoint
        # HubSpot expects comma-separated strings for list params
        params: dict[str, Any] = {
            "limit": limit,
            "properties": ",".join(HUBSPOT_NOTE_PROPERTIES),
            "associations": ",".join(HUBSPOT_NOTE_ASSOCIATIONS),
        }

        active = await self._fetch_paginated(
            endpoint="/crm/v3/objects/notes",
            params=params,
        )

        archived_params: dict[str, Any] = dict(params)
        archived_params["archived"] = True
        archived = await self._fetch_paginated(
            endpoint="/crm/v3/objects/notes",
            params=archived_params,
        )

        return [*active, *archived]

    async def _fetch_incremental(
        self,
        last_sync_time: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch notes modified since last sync using search endpoint.

        HubSpot's search endpoint requires POST with filterGroups in the body.
        This is limited to 10,000 results per query.

        Args:
            last_sync_time: Unix timestamp in milliseconds
            limit: Number of records per page (max 100)

        Returns:
            List of note records modified since last_sync_time
        """
        all_records: list[dict[str, Any]] = []
        after: str | None = None
        page = 1

        logger.info(
            "Starting incremental note fetch with hs_lastmodifieddate >= %s",
            last_sync_time,
        )

        while True:
            # Build search request body
            body: dict[str, Any] = {
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "hs_lastmodifieddate",
                                "operator": "GTE",
                                "value": last_sync_time,
                            }
                        ]
                    }
                ],
                "properties": HUBSPOT_NOTE_PROPERTIES,
                "limit": min(limit, 100),  # HubSpot max is 100
            }

            if after:
                body["after"] = after

            response = await self.nango_client.proxy_request(
                provider_config_key=self.provider_config_key,
                connection_id=self.connection_id,
                endpoint="/crm/v3/objects/notes/search",
                method="POST",
                data=body,
            )

            if not response or not isinstance(response, dict):
                break

            records = response.get("results", [])
            if not records:
                break

            all_records.extend(records)
            logger.debug("Fetched %d notes on page %d", len(records), page)

            # Check pagination
            paging = response.get("paging", {})
            next_page = paging.get("next", {})
            after = next_page.get("after")

            if not after:
                break

            page += 1

            # Safety limit: HubSpot search is limited to 10,000 results
            if len(all_records) >= 10000:
                logger.warning(
                    "Reached HubSpot search limit of 10,000 results. "
                    "Some records may be missing. Consider reducing sync interval."
                )
                break

        logger.info(
            "Completed incremental note fetch: %d records across %d pages",
            len(all_records),
            page,
        )

        return all_records
