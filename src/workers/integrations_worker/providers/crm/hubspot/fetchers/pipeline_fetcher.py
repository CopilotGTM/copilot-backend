"""HubSpot pipeline fetcher for CRM integrations."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.providers.crm.hubspot.fetchers.base import HubSpotDirectFetcher

logger = get_logger(__name__)


class HubSpotPipelineFetcher(HubSpotDirectFetcher):
    """Fetches pipeline records from HubSpot via Nango.

    HubSpot pipelines represent deal workflows with stages.
    Each pipeline contains nested stages data.
    """

    async def fetch_all(self, *, incremental: bool = False) -> list[dict[str, Any]]:
        """Fetch all pipelines from HubSpot.

        Args:
            incremental: If True, only fetch pipelines modified since last sync
                        (Note: HubSpot pipelines API doesn't support filtering by date)

        Returns:
            List of raw pipeline records from HubSpot API
        """
        logger.info(
            "Fetching pipelines from HubSpot (connection=%s, incremental=%s)",
            self.connection_id,
            incremental,
        )

        # HubSpot pipelines endpoint for deals
        response = await self._make_request(
            endpoint="/crm/v3/pipelines/deals",
            method="GET",
            params={},
        )

        if not response:
            logger.warning("No response from HubSpot pipelines API")
            return []

        # Extract pipelines from response
        pipelines = self._extract_records_from_response(response)

        logger.info(
            "Fetched %d pipelines from HubSpot for connection %s",
            len(pipelines),
            self.connection_id,
        )

        return pipelines

    async def fetch_by_ids(self, pipeline_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch specific pipelines by IDs.

        Args:
            pipeline_ids: List of pipeline external IDs to fetch

        Returns:
            List of raw pipeline records

        Note:
            HubSpot pipelines API doesn't support batch fetch by ID.
            This method fetches all pipelines and filters locally.
        """
        if not pipeline_ids:
            return []

        logger.info(
            "Fetching %d specific pipelines from HubSpot (connection=%s)",
            len(pipeline_ids),
            self.connection_id,
        )

        # Fetch all pipelines (HubSpot doesn't support filtering by ID)
        all_pipelines = await self.fetch_all()

        # Filter to requested IDs
        pipeline_id_set = set(pipeline_ids)
        filtered = [
            pipeline for pipeline in all_pipelines if str(pipeline.get("id")) in pipeline_id_set
        ]

        logger.info(
            "Filtered to %d pipelines from %d total",
            len(filtered),
            len(all_pipelines),
        )

        return filtered

    def _extract_records_from_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract pipeline records from HubSpot API response.

        Args:
            response: Raw API response

        Returns:
            List of pipeline records
        """
        # HubSpot pipelines endpoint returns: {"results": [...]}
        if isinstance(response, dict) and "results" in response:
            results = response["results"]
            if isinstance(results, list):
                return [item for item in results if isinstance(item, dict)]

        # Fallback to base class implementation
        return super()._extract_records_from_response(response)
