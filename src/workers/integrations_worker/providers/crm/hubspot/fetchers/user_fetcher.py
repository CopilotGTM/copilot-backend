"""HubSpot user/owner fetcher for CRM integrations."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.providers.crm.hubspot.fetchers.base import HubSpotDirectFetcher

logger = get_logger(__name__)


class HubSpotUserFetcher(HubSpotDirectFetcher):
    """Fetches user/owner records from HubSpot via Nango.

    HubSpot users represent team members who can own records,
    be assigned tasks, and manage deals/contacts.
    """

    async def fetch_all(self, *, incremental: bool = False) -> list[dict[str, Any]]:
        """Fetch all users from HubSpot.

        Args:
            incremental: If True, only fetch users modified since last sync
                        (Note: HubSpot owners API doesn't support filtering by modified date)

        Returns:
            List of raw user records from HubSpot API
        """
        logger.info(
            "Fetching users from HubSpot (connection=%s, incremental=%s)",
            self.connection_id,
            incremental,
        )

        # HubSpot owners endpoint returns all owners (no pagination needed typically)
        # The API returns a simple array of owner objects
        response = await self._make_request(
            endpoint="/crm/v3/owners",
            method="GET",
            params={},
        )

        if not response:
            logger.warning("No response from HubSpot owners API")
            return []

        # Extract users from response
        users = self._extract_records_from_response(response)

        logger.info(
            "Fetched %d users from HubSpot for connection %s",
            len(users),
            self.connection_id,
        )

        return users

    async def fetch_by_ids(self, user_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch specific users by IDs.

        Args:
            user_ids: List of user external IDs to fetch

        Returns:
            List of raw user records

        Note:
            HubSpot owners API doesn't support batch fetch by ID.
            This method fetches all users and filters locally.
        """
        if not user_ids:
            return []

        logger.info(
            "Fetching %d specific users from HubSpot (connection=%s)",
            len(user_ids),
            self.connection_id,
        )

        # Fetch all users (HubSpot doesn't support filtering by ID)
        all_users = await self.fetch_all()

        # Filter to requested IDs
        user_id_set = set(user_ids)
        filtered = [user for user in all_users if str(user.get("id")) in user_id_set]

        logger.info(
            "Filtered to %d users from %d total",
            len(filtered),
            len(all_users),
        )

        return filtered

    def _extract_records_from_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract user records from HubSpot API response.

        Args:
            response: Raw API response

        Returns:
            List of user records
        """
        # HubSpot owners endpoint returns: {"results": [...]}
        if isinstance(response, dict) and "results" in response:
            results = response["results"]
            if isinstance(results, list):
                return [item for item in results if isinstance(item, dict)]

        # Fallback to base class implementation
        return super()._extract_records_from_response(response)
