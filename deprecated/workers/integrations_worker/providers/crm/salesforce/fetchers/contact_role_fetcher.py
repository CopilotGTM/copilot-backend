"""Salesforce OpportunityContactRole fetcher.

Fetches OpportunityContactRole records that link Contacts to Opportunities
with roles (e.g., "Decision Maker", "Economic Buyer").
"""

from __future__ import annotations

from typing import Any

from .base import SalesforceSOQLFetcher

# OpportunityContactRole fields to query
# See: https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_opportunitycontactrole.htm
SOQL_FIELDS = [
    "Id",
    "OpportunityId",
    "ContactId",
    "Role",
    "IsPrimary",
    "CreatedDate",
    "LastModifiedDate",
]


class SalesforceContactRoleFetcher(SalesforceSOQLFetcher):
    """Fetches OpportunityContactRole records via SOQL."""

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_synced_at: Any = None,
    ) -> list[dict[str, Any]]:
        """Fetch all OpportunityContactRole records.

        Args:
            incremental: Whether to fetch only changes since last_synced_at.
            last_synced_at: Timestamp for incremental filtering.

        Returns:
            List of raw OpportunityContactRole records from Salesforce.
        """
        # SOQL query - fields are hardcoded constants, not user input
        fields = ", ".join(SOQL_FIELDS)
        soql = f"SELECT {fields} FROM OpportunityContactRole"  # noqa: S608  # nosec B608

        if incremental and last_synced_at:
            soql = self._add_incremental_where(soql, last_synced_at=last_synced_at)

        soql += " ORDER BY LastModifiedDate ASC"
        return await self._query_all(soql)


__all__ = ["SalesforceContactRoleFetcher"]
