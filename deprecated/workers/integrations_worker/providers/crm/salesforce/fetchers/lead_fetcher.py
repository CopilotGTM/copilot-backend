"""Salesforce Lead fetcher."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import SalesforceSOQLFetcher


class SalesforceLeadFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce Lead records."""

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_synced_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT Id, FirstName, LastName, Name, Email, Phone, MobilePhone, Title, "
            "Company, Status, LeadSource, OwnerId, CreatedDate, LastModifiedDate, IsDeleted "
            "FROM Lead"
        )

        if incremental:
            query = self._add_incremental_where(query, last_synced_at=last_synced_at)

        return await self._query_all(query)
