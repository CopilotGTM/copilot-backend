"""Salesforce pipeline/stage fetcher.

Salesforce doesn't have a direct 'pipeline' object like HubSpot.
A practical starting point for stage metadata is OpportunityStage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import SalesforceSOQLFetcher


class SalesforcePipelineFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce Opportunity stage metadata."""

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_synced_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        # OpportunityStage is fairly static; incremental filtering isn't necessary.
        _ = incremental, last_synced_at

        query = (
            "SELECT Id, MasterLabel, ApiName, SortOrder, IsActive, IsClosed, IsWon, DefaultProbability "
            "FROM OpportunityStage"
        )
        return await self._query_all(query)
