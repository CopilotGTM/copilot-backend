"""HubSpot Deal Task data fetcher."""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.providers.crm.hubspot.fetchers.base import HubSpotDirectFetcher

logger = get_logger(__name__)

# HubSpot task properties to fetch
HUBSPOT_DEAL_TASK_PROPERTIES = [
    "hs_task_subject",
    "hs_task_body",
    "hs_task_status",
    "hs_task_type",
    "hs_task_priority",
    "hubspot_owner_id",
    "hubspot_owner_email",
    "hubspot_owner_name",
    "hs_task_completion_date",
    "hs_timestamp",
    "hs_lastmodifieddate",
    "hs_createdate",
    "hs_task_reminders",
    "hs_task_is_all_day",
    "hs_task_send_default_reminder",
    "hs_task_repeat_interval",
    "hs_task_for_object_type",
    "hs_object_id",
]

# Associations to include with tasks
HUBSPOT_DEAL_TASK_ASSOCIATIONS = ["deals", "contacts", "companies"]


class HubSpotDealTaskFetcher(HubSpotDirectFetcher):
    """Fetcher for HubSpot task data (supports both deal-associated and standalone tasks)."""

    async def fetch_all(
        self,
        *,
        limit: int = 100,
        incremental: bool = False,
        last_sync_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all tasks from HubSpot, including those with and without deal associations.

        Args:
            limit: Number of records per page
            incremental: Whether to fetch only updated records
            last_sync_time: Unix timestamp in milliseconds for incremental sync

        Returns:
            List of task records with properties and associations
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
            "properties": ",".join(HUBSPOT_DEAL_TASK_PROPERTIES),
            "associations": ",".join(HUBSPOT_DEAL_TASK_ASSOCIATIONS),
        }

        # Fetch active tasks first (default)
        active = await self._fetch_paginated(
            endpoint="/crm/v3/objects/tasks",
            params=params,
        )

        # Then fetch archived tasks, so we can propagate deletions
        archived_params: dict[str, Any] = dict(params)
        archived_params["archived"] = True
        archived = await self._fetch_paginated(
            endpoint="/crm/v3/objects/tasks",
            params=archived_params,
        )

        all_tasks = [*active, *archived]

        # Note: We return ALL tasks, not just those with deal associations.
        # Tasks can be created without deals and associated later,
        # and the model supports tasks without deal_id (nullable field).
        return all_tasks

    async def _fetch_incremental(
        self,
        last_sync_time: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch tasks modified since last sync using search endpoint.

        HubSpot's search endpoint requires POST with filterGroups in the body.
        This is limited to 10,000 results per query.

        Args:
            last_sync_time: Unix timestamp in milliseconds
            limit: Number of records per page (max 100)

        Returns:
            List of task records modified since last_sync_time
        """
        all_records: list[dict[str, Any]] = []
        after: str | None = None
        page = 1

        logger.info(
            "Starting incremental task fetch with hs_lastmodifieddate >= %s",
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
                "properties": HUBSPOT_DEAL_TASK_PROPERTIES,
                "limit": min(limit, 100),  # HubSpot max is 100
            }

            if after:
                body["after"] = after

            response = await self._make_request(
                endpoint="/crm/v3/objects/tasks/search",
                method="POST",
                data=body,
            )

            if not response or not isinstance(response, dict):
                break

            records = response.get("results", [])
            if not records:
                break

            all_records.extend(records)
            logger.debug("Fetched %d tasks on page %d", len(records), page)

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
            "Completed incremental task fetch: %d records across %d pages",
            len(all_records),
            page,
        )

        return all_records

    async def fetch_by_ids(
        self,
        task_ids: list[str],
        *,
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch specific tasks by their IDs.

        Args:
            task_ids: List of HubSpot task IDs
            batch_size: Number of records per batch request

        Returns:
            List of task records
        """
        return await self._fetch_batch(
            endpoint="/crm/v3/objects/tasks/batch/read",
            entity_ids=task_ids,
            batch_size=batch_size,
        )

    async def fetch_by_deal(
        self,
        deal_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch tasks associated with a specific deal.

        Args:
            deal_id: HubSpot deal ID
            limit: Number of records per page

        Returns:
            List of task records
        """
        # Use HubSpot associations API
        endpoint = f"/crm/v3/objects/deals/{deal_id}/associations/tasks"
        associations = await self._fetch_paginated(
            endpoint=endpoint,
            params={"limit": limit},
        )

        # Extract task IDs and fetch full task data
        task_ids = [str(assoc.get("id")) for assoc in associations if assoc.get("id")]
        if not task_ids:
            return []

        return await self.fetch_by_ids(task_ids)

    def _has_deal_association(self, task: dict[str, Any]) -> bool:
        """Check if task has deal associations.

        HubSpot returns associations in format: {"deals": {"results": [...]}}

        Args:
            task: Task record

        Returns:
            True if task has at least one deal association
        """
        associations = task.get("associations", {})
        if not isinstance(associations, dict):
            return False

        deals_data = associations.get("deals", {})

        # Handle both direct list format and results format
        if isinstance(deals_data, dict):
            deals = deals_data.get("results", [])
        elif isinstance(deals_data, list):
            deals = deals_data
        else:
            return False

        return isinstance(deals, list) and len(deals) > 0
