"""HubSpot owner/user fetcher."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.providers.crm.hubspot.fetchers.base import HubSpotDirectFetcher

logger = get_logger(__name__)


class HubSpotOwnerFetcher(HubSpotDirectFetcher):
    """Fetches owner data from HubSpot CRM.

    Owners in HubSpot are users who can be assigned to records (deals, contacts, etc.).
    This fetcher retrieves owner information including:
    - Basic user info (name, email)
    - Team assignments
    - Role and permissions
    - Activity status
    """

    async def fetch_all(
        self,
        *,
        incremental: bool = False,  # noqa: ARG002 - Interface requirement
    ) -> list[dict[str, Any]]:
        """Fetch all owners from HubSpot.

        Args:
            incremental: If True, filter by recent updates (not supported for owners)

        Returns:
            List of owner records
        """
        endpoint = "/crm/v3/owners"
        params: dict[str, Any] = {
            "limit": 100,  # HubSpot max per page
        }

        # Note: HubSpot owners endpoint doesn't support filtering by update date
        # Incremental sync not applicable for owner data (relatively static)

        return await self._fetch_paginated(endpoint, params)

    async def fetch_by_ids(self, entity_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch specific owners by ID.

        Args:
            entity_ids: List of owner IDs

        Returns:
            List of owner records
        """
        if not entity_ids:
            return []

        # HubSpot doesn't have batch owner endpoint
        # Fetch individual owners
        records: list[dict[str, Any]] = []

        for owner_id in entity_ids:
            try:
                response = await self._make_request(
                    endpoint=f"/crm/v3/owners/{owner_id}",
                    method="GET",
                )

                if response and isinstance(response, dict):
                    records.append(response)

            except Exception as e:
                # Skip failed individual fetches
                logger.debug("Failed to fetch owner %s: %s", owner_id, str(e))
                continue

        return records

    def _extract_records_from_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract owner records from HubSpot API response.

        Args:
            response: Raw API response

        Returns:
            List of owner records
        """
        # HubSpot owners response structure
        if "results" in response and isinstance(response["results"], list):
            return response["results"]

        return []

    def _has_next_page(self, response: dict[str, Any]) -> bool:
        """Check if more pages are available.

        Args:
            response: Current page response

        Returns:
            True if next page exists
        """
        paging = response.get("paging")
        if paging:
            return "next" in paging and paging["next"] is not None

        return False

    def _get_next_page_params(
        self,
        response: dict[str, Any],
        current_params: dict[str, Any],
    ) -> dict[str, Any]:
        """Get parameters for next page request.

        Args:
            response: Current page response
            current_params: Current request parameters

        Returns:
            Updated parameters with pagination cursor
        """
        params = current_params.copy()

        paging = response.get("paging")
        if paging and "next" in paging:
            next_page = paging["next"]
            if "after" in next_page:
                params["after"] = next_page["after"]

        return params
