"""HubSpot Note data fetcher.

Fetches HubSpot CRM notes. Notes are later normalized into our canonical
CRM activity model (crm_activities) as activity_type=NOTE.

We intentionally keep this fetcher focused on retrieval; any cross-provider
normalization happens in the provider wrapper / sync services.
"""

from __future__ import annotations

from typing import Any

from src.workers.integrations_worker.core import BaseFetcher

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
        params: dict[str, Any] = {
            "limit": limit,
            "properties": HUBSPOT_NOTE_PROPERTIES,
            "associations": HUBSPOT_NOTE_ASSOCIATIONS,
        }

        if incremental and last_sync_time:
            # NOTE: HubSpot's official way is to use /search (POST), but the
            # existing integration fetchers pass filterGroups via proxy.
            params["filterGroups"] = [
                {
                    "filters": [
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "GTE",
                            "value": last_sync_time,
                        }
                    ]
                }
            ]

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
