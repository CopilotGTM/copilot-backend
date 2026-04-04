"""HubSpot Company data fetcher."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.providers.crm.hubspot.fetchers.base import HubSpotDirectFetcher

logger = get_logger(__name__)

# HubSpot company properties to fetch
HUBSPOT_COMPANY_PROPERTIES = [
    "name",
    "domain",
    "website",
    "description",
    "industry",
    "type",
    "lifecyclestage",
    "hubspot_owner_id",
    "hubspot_owner_email",
    "hubspot_owner_name",
    "numberofemployees",
    "annualrevenue",
    "phone",
    "address",
    "address2",
    "city",
    "state",
    "country",
    "zip",
    "timezone",
    "linkedin_company_page",
    "linkedinbio",
    "createdate",
    "hs_lastmodifieddate",
    "hs_object_id",
    "hs_total_deal_value",
    "total_revenue",
    "total_money_raised",
]

# Associations to include with companies
HUBSPOT_COMPANY_ASSOCIATIONS = ["contacts", "deals"]


class HubSpotCompanyFetcher(HubSpotDirectFetcher):
    """Fetcher for HubSpot company data."""

    async def fetch_all(
        self,
        *,
        limit: int = 100,
        incremental: bool = False,
        last_sync_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all companies from HubSpot.

        Args:
            limit: Number of records per page
            incremental: Whether to fetch only updated records
            last_sync_time: Unix timestamp in milliseconds for incremental sync

        Returns:
            List of company records with properties and associations
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
            "properties": ",".join(HUBSPOT_COMPANY_PROPERTIES),
            "associations": ",".join(HUBSPOT_COMPANY_ASSOCIATIONS),
        }

        return await self._fetch_paginated(
            endpoint="/crm/v3/objects/companies",
            params=params,
        )

    async def _fetch_incremental(
        self,
        last_sync_time: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch companies modified since last sync using search endpoint.

        HubSpot's search endpoint requires POST with filterGroups in the body.
        This is limited to 10,000 results per query.

        Args:
            last_sync_time: Unix timestamp in milliseconds
            limit: Number of records per page (max 100)

        Returns:
            List of company records modified since last_sync_time
        """
        all_records: list[dict[str, Any]] = []
        after: str | None = None
        page = 1

        logger.info(
            "Starting incremental company fetch with hs_lastmodifieddate >= %s",
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
                "properties": HUBSPOT_COMPANY_PROPERTIES,
                "limit": min(limit, 100),  # HubSpot max is 100
            }

            if after:
                body["after"] = after

            response = await self._make_request(
                endpoint="/crm/v3/objects/companies/search",
                method="POST",
                data=body,
            )

            if not response or not isinstance(response, dict):
                break

            records = response.get("results", [])
            if not records:
                break

            all_records.extend(records)
            logger.debug("Fetched %d companies on page %d", len(records), page)

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
            "Completed incremental company fetch: %d records across %d pages",
            len(all_records),
            page,
        )

        return all_records

    async def fetch_by_ids(
        self,
        company_ids: list[str],
        *,
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch specific companies by their IDs.

        Args:
            company_ids: List of HubSpot company IDs
            batch_size: Number of records per batch request

        Returns:
            List of company records
        """
        return await self._fetch_batch(
            endpoint="/crm/v3/objects/companies/batch/read",
            entity_ids=company_ids,
            batch_size=batch_size,
        )

    async def fetch_by_domain(
        self,
        domain: str,
    ) -> list[dict[str, Any]]:
        """Fetch companies by domain name.

        Args:
            domain: Domain name to search for

        Returns:
            List of matching company records
        """
        params = {
            "properties": HUBSPOT_COMPANY_PROPERTIES,
            "associations": HUBSPOT_COMPANY_ASSOCIATIONS,
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "domain",
                            "operator": "EQ",
                            "value": domain,
                        }
                    ]
                }
            ],
        }

        return await self._fetch_paginated(
            endpoint="/crm/v3/objects/companies/search",
            params=params,
        )
