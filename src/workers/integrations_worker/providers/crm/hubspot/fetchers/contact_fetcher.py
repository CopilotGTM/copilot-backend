"""HubSpot Contact data fetcher."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.core import BaseFetcher

logger = get_logger(__name__)

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
            last_sync_time: Unix timestamp in milliseconds for incremental sync

        Returns:
            List of contact records with properties and associations
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
            "properties": ",".join(HUBSPOT_CONTACT_PROPERTIES),
            "associations": ",".join(HUBSPOT_CONTACT_ASSOCIATIONS),
        }

        logger.info(
            "Fetching contacts with properties: %s",
            params["properties"][:200],  # First 200 chars
        )

        records = await self._fetch_paginated(
            endpoint="/crm/v3/objects/contacts",
            params=params,
        )

        # Log sample of first contact's properties for debugging
        if records:
            first_contact = records[0]
            props = first_contact.get("properties", {})
            logger.info(
                "Sample contact from HubSpot: id=%s, hubspot_owner_id=%r, "
                "hubspot_owner_email=%r, property_keys=%s",
                first_contact.get("id"),
                props.get("hubspot_owner_id"),
                props.get("hubspot_owner_email"),
                list(props.keys()),
            )

        return records

    async def _fetch_incremental(
        self,
        last_sync_time: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch contacts modified since last sync using search endpoint.

        HubSpot's search endpoint requires POST with filterGroups in the body.
        This is limited to 10,000 results per query.

        Args:
            last_sync_time: Unix timestamp in milliseconds
            limit: Number of records per page (max 100)

        Returns:
            List of contact records modified since last_sync_time
        """
        all_records: list[dict[str, Any]] = []
        after: str | None = None
        page = 1

        logger.info(
            "Starting incremental contact fetch with hs_lastmodifieddate >= %s",
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
                "properties": HUBSPOT_CONTACT_PROPERTIES,
                "limit": min(limit, 100),  # HubSpot max is 100
            }

            if after:
                body["after"] = after

            response = await self.nango_client.proxy_request(
                provider_config_key=self.provider_config_key,
                connection_id=self.connection_id,
                endpoint="/crm/v3/objects/contacts/search",
                method="POST",
                data=body,
            )

            if not response or not isinstance(response, dict):
                break

            records = response.get("results", [])
            if not records:
                break

            all_records.extend(records)
            logger.debug("Fetched %d contacts on page %d", len(records), page)

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
            "Completed incremental contact fetch: %d records across %d pages",
            len(all_records),
            page,
        )

        return all_records

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
