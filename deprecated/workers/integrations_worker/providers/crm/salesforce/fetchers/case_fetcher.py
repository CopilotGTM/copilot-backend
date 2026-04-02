"""Salesforce Case (Ticket) fetcher.

Salesforce Cases are the equivalent of HubSpot Tickets - they represent
customer support requests, issues, or service tickets.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.service.integrations.nango_client import NangoConnectionError

from .base import SalesforceSOQLFetcher


class SalesforceCaseFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce Case (Ticket) records.

    Handles schema variations across Salesforce orgs by retrying
    with optional fields removed when INVALID_FIELD errors occur.
    """

    # Fields that every Case object should have.
    _REQUIRED_FIELDS: tuple[str, ...] = (
        "Id",
        "CaseNumber",
        "Subject",
        "Description",
        "Status",
        "Priority",
        "Origin",
        "Type",
        "Reason",
        "OwnerId",
        "ContactId",
        "AccountId",
        "CreatedDate",
        "LastModifiedDate",
        "IsClosed",
        "IsDeleted",
    )

    # Owner relationship fields - fetches owner details without separate User query
    _OWNER_FIELDS: tuple[str, ...] = (
        "Owner.Id",
        "Owner.Name",
        "Owner.Email",
        "Owner.FirstName",
        "Owner.LastName",
    )

    # Fields that may not exist in all orgs.
    _OPTIONAL_FIELDS: tuple[str, ...] = (
        # Some orgs may not have these enabled
        "ClosedDate",
        "IsEscalated",
        "SuppliedName",
        "SuppliedEmail",
        "SuppliedPhone",
        "SuppliedCompany",
        # Parent case for case hierarchies
        "ParentId",
        # Web-to-case fields
        "Comments",
    )

    @staticmethod
    def _build_query(fields: list[str]) -> str:
        # SOQL query - fields are hardcoded constants, not user input
        field_list = ", ".join(fields)
        return f"SELECT {field_list} FROM Case"  # noqa: S608  # nosec B608

    @staticmethod
    def _extract_missing_field(message: str) -> str | None:
        match = re.search(r"No such column '([^']+)'", message)
        return match.group(1) if match else None

    async def fetch_all(
        self,
        *,
        incremental: bool = False,
        last_synced_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        # Include owner relationship fields to get owner details in same query
        fields = [*self._REQUIRED_FIELDS, *self._OWNER_FIELDS, *self._OPTIONAL_FIELDS]
        attempts_remaining = 7  # More attempts due to more optional/relationship fields

        while True:
            query = self._build_query(fields)
            if incremental:
                query = self._add_incremental_where(query, last_synced_at=last_synced_at)

            try:
                return await self._query_all(query)
            except NangoConnectionError as exc:
                attempts_remaining -= 1
                if attempts_remaining <= 0:
                    raise

                missing_field = self._extract_missing_field(str(exc))
                if not missing_field:
                    raise

                # Never drop required fields; if a required field is missing,
                # the org schema is incompatible with this sync.
                if missing_field in self._REQUIRED_FIELDS:
                    # Try removing from required and continue - some orgs may not have all
                    # "standard" fields if they've been customized
                    if missing_field in fields:
                        fields = [f for f in fields if f != missing_field]
                        continue
                    raise

                if missing_field in fields:
                    fields = [f for f in fields if f != missing_field]
                    continue

                raise
