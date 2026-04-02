"""Salesforce Contact fetcher."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import SalesforceSOQLFetcher


class SalesforceContactFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce Contact records."""

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_synced_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT Id, FirstName, LastName, Name, Email, Phone, MobilePhone, Title, "
            "Department, MailingStreet, MailingCity, MailingState, MailingPostalCode, "
            "MailingCountry, AccountId, OwnerId, LeadSource, CreatedDate, "
            "LastModifiedDate, IsDeleted "
            "FROM Contact"
        )

        if incremental:
            query = self._add_incremental_where(query, last_synced_at=last_synced_at)

        return await self._query_all(query)
