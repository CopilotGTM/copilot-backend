"""Salesforce Account (Company) fetcher."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import SalesforceSOQLFetcher

# Owner relationship fields to embed owner details directly in Account query
_OWNER_FIELDS = (
    "Owner.Id",
    "Owner.Name",
    "Owner.Email",
)


class SalesforceAccountFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce Account records."""

    _SOQL_FIELDS = (
        "Id",
        "Name",
        "Website",
        "Phone",
        "Industry",
        "NumberOfEmployees",
        "OwnerId",
        "Description",
        "Type",
        "BillingStreet",
        "BillingCity",
        "BillingState",
        "BillingPostalCode",
        "BillingCountry",
        "ShippingStreet",
        "ShippingCity",
        "ShippingState",
        "ShippingPostalCode",
        "ShippingCountry",
        "CreatedDate",
        "LastModifiedDate",
        "IsDeleted",
    )

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_synced_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        # SOQL query (Salesforce Object Query Language).
        # Field names are hardcoded constants (no user input), but Ruff/Bandit can
        # still flag string-based query construction as a potential injection vector.
        fields = (*self._SOQL_FIELDS, *_OWNER_FIELDS)
        query = "SELECT " + ", ".join(fields) + " FROM Account"  # noqa: S608  # nosec B608

        if incremental:
            query = self._add_incremental_where(query, last_synced_at=last_synced_at)

        return await self._query_all(query)
