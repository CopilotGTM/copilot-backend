"""HubSpot Deal data fetcher."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.core import BaseFetcher

logger = get_logger(__name__)

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
    "closed_won_reason",
    "hs_closed_reason",
    "hs_expected_close_date",
    "lastactivitydate",
    "lead_source",
    "hs_lead_source",
    "hs_arr",
    "hs_mrr",
    "hs_tcv",
    "hs_acv",
    # Standard HubSpot Ownership & Collaboration
    "hubspot_team_id",
    "hs_all_collaborator_owner_ids",  # Standard field for deal collaborators
    "hs_all_deal_split_owner_ids",  # Standard field for deal splits
    # Useful additional defaults from HubSpot
    "hs_is_closed_won",
    "hs_is_closed_lost",
    "hs_is_closed",
    "days_to_close",
    "hs_created_by_user_id",
    "hs_updated_by_user_id",
    "hs_recurring_revenue_amount",
    "hs_recurring_revenue_deal_type",
    "hs_recurring_revenue_inactive_reason",
    "hs_recurring_revenue_end_date",
    "closed_amount",
    "hs_projected_amount",
    "hs_projected_amount_in_home_currency",
    "hs_exchange_rate",
    "hs_time_in_current_stage",
    # Association/count helper properties (if enabled in portal)
    "num_associated_contacts",
    "num_associated_companies",
    "num_notes",
]

# Associations to include with deals
HUBSPOT_DEAL_ASSOCIATIONS = ["contacts", "companies", "line_items"]

# Deal property history to request for richer stage change context
HUBSPOT_DEAL_PROPERTY_HISTORY = [
    "dealstage",
    "pipeline",
    "amount",
    "closedate",
    "hs_deal_stage_probability",
    "hs_forecast_probability",
]


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
            last_sync_time: Unix timestamp in milliseconds for incremental sync

        Returns:
            List of deal records with properties and associations
        """
        if incremental and last_sync_time:
            # Use search endpoint for incremental sync (requires POST)
            return await self._fetch_incremental(
                last_sync_time=last_sync_time,
                limit=limit,
            )

        # Full sync uses the standard GET endpoint
        max_limit = min(limit, 50)  # HubSpot caps propertiesWithHistory requests to 50 rows

        # HubSpot expects comma-separated strings for list params
        params: dict[str, Any] = {
            "limit": max_limit,
            "properties": ",".join(HUBSPOT_DEAL_PROPERTIES),
            "associations": ",".join(HUBSPOT_DEAL_ASSOCIATIONS),
            "propertiesWithHistory": ",".join(HUBSPOT_DEAL_PROPERTY_HISTORY),
        }

        # Fetch active deals first (default)
        active = await self._fetch_paginated(
            endpoint="/crm/v3/objects/deals",
            params=params,
        )

        # Then fetch archived deals, so we can propagate deletions
        archived_params: dict[str, Any] = dict(params)
        archived_params["archived"] = True
        archived = await self._fetch_paginated(
            endpoint="/crm/v3/objects/deals",
            params=archived_params,
        )

        return [*active, *archived]

    async def _fetch_incremental(
        self,
        last_sync_time: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch deals modified since last sync using search endpoint.

        HubSpot's search endpoint requires POST with filterGroups in the body.
        This is limited to 10,000 results per query.

        Args:
            last_sync_time: Unix timestamp in milliseconds
            limit: Number of records per page (max 100, but we use 50 for propertiesWithHistory)

        Returns:
            List of deal records modified since last_sync_time
        """
        all_records: list[dict[str, Any]] = []
        after: str | None = None
        page = 1
        max_limit = min(limit, 50)  # HubSpot caps propertiesWithHistory requests to 50 rows

        logger.info(
            "Starting incremental deal fetch with hs_lastmodifieddate >= %s",
            last_sync_time,
        )

        while True:
            # Build search request body
            # Note: HubSpot search supports propertiesWithHistory for stage tracking
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
                "properties": HUBSPOT_DEAL_PROPERTIES,
                "propertiesWithHistory": HUBSPOT_DEAL_PROPERTY_HISTORY,
                "associations": HUBSPOT_DEAL_ASSOCIATIONS,
                "limit": max_limit,
            }

            if after:
                body["after"] = after

            response = await self.nango_client.proxy_request(
                provider_config_key=self.provider_config_key,
                connection_id=self.connection_id,
                endpoint="/crm/v3/objects/deals/search",
                method="POST",
                data=body,
            )

            if not response or not isinstance(response, dict):
                break

            records = response.get("results", [])
            if not records:
                break

            all_records.extend(records)
            logger.debug("Fetched %d deals on page %d", len(records), page)

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
            "Completed incremental deal fetch: %d records across %d pages",
            len(all_records),
            page,
        )

        return all_records

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
