"""Salesforce User fetcher."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import SalesforceSOQLFetcher


class SalesforceUserFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce User records."""

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_synced_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT Id, Name, FirstName, LastName, Email, Username, IsActive, UserType, "
            "ProfileId, CreatedDate, LastModifiedDate "
            "FROM User"
        )

        if incremental:
            query = self._add_incremental_where(query, last_synced_at=last_synced_at)

        return await self._query_all(query)
