"""Base fetcher class for external API data retrieval."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)


class BaseFetcher(ABC):
    """Abstract base class for provider-specific data fetchers.

    This class provides common functionality for fetching data from
    external APIs via Nango proxy, including:
    - Pagination handling
    - Batch requests
    - Error handling
    - Rate limiting awareness

    Subclasses must implement:
    - fetch_all(): Fetch all records with optional incremental filtering
    - fetch_by_ids(): Fetch specific records by ID (optional)
    """

    def __init__(
        self,
        nango_client: NangoClient,
        provider_config_key: str,
        connection_id: str,
    ):
        """Initialize fetcher.

        Args:
            nango_client: NangoClient instance
            provider_config_key: Nango provider configuration key
            connection_id: Nango connection ID
        """
        self.nango_client = nango_client
        self.provider_config_key = provider_config_key
        self.connection_id = connection_id

    @abstractmethod
    async def fetch_all(self, *, incremental: bool = False) -> list[dict[str, Any]]:
        """Fetch all records from provider.

        Args:
            incremental: If True, only fetch records modified since last sync

        Returns:
            List of raw records from provider API
        """
        pass

    async def fetch_by_ids(self, entity_ids: list[str]) -> list[dict[str, Any]]:  # noqa: ARG002
        """Fetch specific records by IDs.

        Args:
            entity_ids: List of external IDs to fetch

        Returns:
            List of raw records

        Note:
            Default implementation returns empty list.
            Override in subclass if provider supports batch fetch by ID.
        """
        # Use await to maintain proper async interface
        # This allows event loop to process other tasks even though this is a no-op
        await asyncio.sleep(0)

        logger.warning(
            "%s does not implement fetch_by_ids, returning empty list",
            self.__class__.__name__,
        )
        return []

    async def _fetch_paginated(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a paginated endpoint.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            max_pages: Maximum pages to fetch (None = all)

        Returns:
            Combined list of all records across pages
        """
        all_records: list[dict[str, Any]] = []
        params = params or {}
        page = 1

        while True:
            if max_pages and page > max_pages:
                break

            logger.debug(
                "Fetching page %d from %s for connection %s",
                page,
                endpoint,
                self.connection_id,
            )

            response = await self.nango_client.proxy_request(
                provider_config_key=self.provider_config_key,
                connection_id=self.connection_id,
                endpoint=endpoint,
                method="GET",
                params=params,
            )

            if not response or not isinstance(response, dict):
                break

            # Extract records (provider-specific logic)
            records = self._extract_records_from_response(response)
            if not records:
                break

            all_records.extend(records)
            logger.debug("Fetched %d records on page %d", len(records), page)

            # Check for pagination
            if not self._has_next_page(response):
                break

            # Update pagination params
            params = self._get_next_page_params(response, params)
            page += 1

        logger.info(
            "Completed paginated fetch from %s: %d total records across %d pages",
            endpoint,
            len(all_records),
            page,
        )

        return all_records

    async def _fetch_batch(
        self,
        endpoint: str,
        entity_ids: list[str],
        *,
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch records in batches.

        Args:
            endpoint: API endpoint for batch requests
            entity_ids: List of IDs to fetch
            batch_size: Number of IDs per batch

        Returns:
            Combined list of all records
        """
        all_records: list[dict[str, Any]] = []

        for i in range(0, len(entity_ids), batch_size):
            batch = entity_ids[i : i + batch_size]

            logger.debug(
                "Fetching batch %d-%d/%d from %s",
                i,
                i + len(batch),
                len(entity_ids),
                endpoint,
            )

            response = await self.nango_client.proxy_request(
                provider_config_key=self.provider_config_key,
                connection_id=self.connection_id,
                endpoint=endpoint,
                method="POST",
                data={"ids": batch},
            )

            if response and isinstance(response, dict):
                records = self._extract_records_from_response(response)
                all_records.extend(records)

        logger.info("Completed batch fetch: %d records", len(all_records))
        return all_records

    def _extract_records_from_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract records array from API response.

        Args:
            response: Raw API response

        Returns:
            List of records

        Note:
            Override in subclass if provider uses different response structure.
            Default checks common patterns: results, data, items, records.
        """
        # Try common response patterns
        for key in ["results", "data", "items", "records"]:
            value = response.get(key)
            if isinstance(value, list):
                # Type narrowing: ensure we return list[dict[str, Any]]
                return [item for item in value if isinstance(item, dict)]

        # If response itself is a list
        if isinstance(response, list):
            # Type narrowing: filter to only dict items
            return [item for item in response if isinstance(item, dict)]

        # Single object wrapped in response
        if "id" in response or "external_id" in response:
            return [response]

        return []

    def _has_next_page(self, response: dict[str, Any]) -> bool:
        """Check if response indicates more pages.

        Args:
            response: API response

        Returns:
            True if more pages available

        Note:
            Override in subclass for provider-specific pagination logic.
        """
        # Common patterns
        paging = response.get("paging") or response.get("pagination")
        if paging:
            return bool(paging.get("next") or paging.get("hasMore"))

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
            Updated parameters for next page

        Note:
            Override in subclass for provider-specific pagination.
        """
        params = current_params.copy()

        # Common patterns
        paging = response.get("paging") or response.get("pagination")
        if paging:
            if "next" in paging:
                params["after"] = paging["next"]
            elif "offset" in paging:
                params["offset"] = paging["offset"]

        return params

    def _build_incremental_filter(self) -> dict[str, Any]:
        """Build filter for incremental sync.

        Returns:
            Filter parameters for API request

        Note:
            Override in subclass to implement incremental sync logic.
            Should filter records modified since last successful sync.
        """
        logger.warning(
            "%s does not implement incremental filtering",
            self.__class__.__name__,
        )
        return {}
