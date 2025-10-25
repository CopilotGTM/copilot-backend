"""HubSpot Deal data fetcher."""

from __future__ import annotations

from typing import Any

from src.workers.integrations_worker.core import BaseFetcher

# HubSpot deal properties to fetch
HUBSPOT_DEAL_PROPERTIES = [
    "dealname",
    "amount",
    "amount_in_home_currency",
    "deal_currency_code",
    "currency",
    "pipeline",
    "dealstage",
    "closedate",
    "createdate",
    "hs_lastmodifieddate",
    "hs_object_id",
    "hubspot_owner_id",
    "hubspot_owner_email",
    "hubspot_owner_name",
    "dealtype",
    "description",
    "hs_priority",
    "hs_deal_stage_probability",
    "hs_forecast_probability",
    "hs_forecast_amount",
    "hs_forecast_category",
    "hs_closed_won_date",
    "hs_closed_lost_date",
    "closed_lost_reason",
    "hs_closed_reason",
    "hs_expected_close_date",
    "lastactivitydate",
    "lead_source",
    "hs_lead_source",
    "hs_arr",
    "hs_mrr",
    "hs_tcv",
    "hs_acv",
]

# Associations to include with deals
HUBSPOT_DEAL_ASSOCIATIONS = ["contacts", "companies", "line_items"]


class HubSpotDealFetcher(BaseFetcher):
    """Fetcher for HubSpot deal data."""

    async def fetch_all(
        self,
        *,
        limit: int = 100,
        incremental: bool = False,
        last_sync_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all deals from HubSpot.

        Args:
            limit: Number of records per page
            incremental: Whether to fetch only updated records
            last_sync_time: ISO timestamp for incremental sync

        Returns:
            List of deal records with properties and associations
        """
        params: dict[str, Any] = {
            "limit": limit,
            "properties": HUBSPOT_DEAL_PROPERTIES,
            "associations": HUBSPOT_DEAL_ASSOCIATIONS,
        }

        if incremental and last_sync_time:
            # HubSpot uses hs_lastmodifieddate for filtering
            # Format: Unix timestamp in milliseconds
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

        return await self._fetch_paginated(
            endpoint="/crm/v3/objects/deals",
            params=params,
        )

    async def fetch_by_ids(
        self,
        deal_ids: list[str],
        *,
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch specific deals by their IDs.

        Args:
            deal_ids: List of HubSpot deal IDs
            batch_size: Number of records per batch request

        Returns:
            List of deal records
        """
        return await self._fetch_batch(
            endpoint="/crm/v3/objects/deals/batch/read",
            entity_ids=deal_ids,
            batch_size=batch_size,
        )

    async def fetch_by_company(
        self,
        company_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch deals associated with a specific company.

        Args:
            company_id: HubSpot company ID
            limit: Number of records per page

        Returns:
            List of deal records
        """
        # Use HubSpot associations API
        endpoint = f"/crm/v3/objects/companies/{company_id}/associations/deals"
        associations = await self._fetch_paginated(
            endpoint=endpoint,
            params={"limit": limit},
        )

        # Extract deal IDs and fetch full deal data
        deal_ids = [str(assoc.get("id")) for assoc in associations if assoc.get("id")]
        if not deal_ids:
            return []

        return await self.fetch_by_ids(deal_ids)

    async def fetch_by_contact(
        self,
        contact_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch deals associated with a specific contact.

        Args:
            contact_id: HubSpot contact ID
            limit: Number of records per page

        Returns:
            List of deal records
        """
        endpoint = f"/crm/v3/objects/contacts/{contact_id}/associations/deals"
        associations = await self._fetch_paginated(
            endpoint=endpoint,
            params={"limit": limit},
        )

        deal_ids = [str(assoc.get("id")) for assoc in associations if assoc.get("id")]
        if not deal_ids:
            return []

        return await self.fetch_by_ids(deal_ids)
