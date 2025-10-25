"""HubSpot Company data fetcher."""

from __future__ import annotations

from typing import Any

from src.workers.integrations_worker.core import BaseFetcher

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


class HubSpotCompanyFetcher(BaseFetcher):
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
            last_sync_time: ISO timestamp for incremental sync

        Returns:
            List of company records with properties and associations
        """
        params: dict[str, Any] = {
            "limit": limit,
            "properties": HUBSPOT_COMPANY_PROPERTIES,
            "associations": HUBSPOT_COMPANY_ASSOCIATIONS,
        }

        if incremental and last_sync_time:
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
            endpoint="/crm/v3/objects/companies",
            params=params,
        )

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
