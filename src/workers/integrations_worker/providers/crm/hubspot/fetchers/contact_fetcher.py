"""HubSpot Contact data fetcher."""

from __future__ import annotations

from typing import Any

from src.workers.integrations_worker.core import BaseFetcher

# HubSpot contact properties to fetch
HUBSPOT_CONTACT_PROPERTIES = [
    "email",
    "hs_additional_emails",
    "additional_emails",
    "firstname",
    "lastname",
    "fullname",
    "jobtitle",
    "title",
    "company",
    "lifecyclestage",
    "hs_lead_status",
    "leadstatus",
    "hs_marketable_status",
    "hs_content_membership_email_status",
    "hubspot_owner_id",
    "hubspot_owner_email",
    "hubspot_owner_name",
    "phone",
    "mobilephone",
    "fax",
    "website",
    "address",
    "city",
    "state",
    "state_region",
    "zip",
    "postalcode",
    "country",
    "timezone",
    "last_contacted",
    "hs_last_contacted",
    "lastactivitydate",
    "hs_lastactivitydate",
    "recent_activity_date",
    "createdate",
    "hs_lastmodifieddate",
    "lastmodifieddate",
    "hs_object_id",
    "hs_email_open",
    "hs_email_click",
    "hs_email_bounce",
    "num_notes",
    "num_contacted_notes",
    "num_conversion_events",
    "hs_analytics_num_page_views",
    "hs_analytics_num_visits",
    "hs_analytics_num_event_completions",
]

# Associations to include with contacts
HUBSPOT_CONTACT_ASSOCIATIONS = ["companies", "deals"]


class HubSpotContactFetcher(BaseFetcher):
    """Fetcher for HubSpot contact data."""

    async def fetch_all(
        self,
        *,
        limit: int = 100,
        incremental: bool = False,
        last_sync_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all contacts from HubSpot.

        Args:
            limit: Number of records per page
            incremental: Whether to fetch only updated records
            last_sync_time: ISO timestamp for incremental sync

        Returns:
            List of contact records with properties and associations
        """
        params: dict[str, Any] = {
            "limit": limit,
            "properties": HUBSPOT_CONTACT_PROPERTIES,
            "associations": HUBSPOT_CONTACT_ASSOCIATIONS,
        }

        if incremental and last_sync_time:
            # HubSpot uses hs_lastmodifieddate for filtering
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
            endpoint="/crm/v3/objects/contacts",
            params=params,
        )

    async def fetch_by_ids(
        self,
        contact_ids: list[str],
        *,
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch specific contacts by their IDs.

        Args:
            contact_ids: List of HubSpot contact IDs
            batch_size: Number of records per batch request

        Returns:
            List of contact records
        """
        return await self._fetch_batch(
            endpoint="/crm/v3/objects/contacts/batch/read",
            entity_ids=contact_ids,
            batch_size=batch_size,
        )

    async def fetch_by_email(
        self,
        email: str,
    ) -> dict[str, Any] | None:
        """Fetch a contact by email address.

        Args:
            email: Email address to search for

        Returns:
            Contact record or None if not found
        """
        params = {
            "properties": HUBSPOT_CONTACT_PROPERTIES,
            "associations": HUBSPOT_CONTACT_ASSOCIATIONS,
        }

        # HubSpot search by email
        response = await self.nango_client.proxy_request(
            provider_config_key=self.provider_config_key,
            connection_id=self.connection_id,
            endpoint=f"/crm/v3/objects/contacts/{email}",
            method="GET",
            params=params,
        )

        return response if isinstance(response, dict) else None

    async def fetch_by_company(
        self,
        company_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch contacts associated with a specific company.

        Args:
            company_id: HubSpot company ID
            limit: Number of records per page

        Returns:
            List of contact records
        """
        endpoint = f"/crm/v3/objects/companies/{company_id}/associations/contacts"
        associations = await self._fetch_paginated(
            endpoint=endpoint,
            params={"limit": limit},
        )

        # Extract contact IDs and fetch full contact data
        contact_ids = [str(assoc.get("id")) for assoc in associations if assoc.get("id")]
        if not contact_ids:
            return []

        return await self.fetch_by_ids(contact_ids)
