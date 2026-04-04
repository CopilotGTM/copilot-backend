"""Salesforce Opportunity (Deal) fetcher."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.service.integrations.nango_client import NangoConnectionError

from .base import SalesforceSOQLFetcher


class SalesforceOpportunityFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce Opportunity records."""

    _REQUIRED_FIELDS: tuple[str, ...] = (
        "Id",
        "Name",
        "Amount",
        "StageName",
        "CloseDate",
        "CreatedDate",
        "LastModifiedDate",
        "OwnerId",
        "AccountId",
        "Probability",
        "IsClosed",
        "IsWon",
        "Type",
        "Description",
        "LeadSource",
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

    _OPTIONAL_FIELDS: tuple[str, ...] = (
        # Multi-currency orgs only.
        "CurrencyIsoCode",
        # Some orgs expose ForecastCategoryName instead of ForecastCategory.
        "ForecastCategory",
    )

    @staticmethod
    def _build_query(fields: list[str]) -> str:
        # SOQL query - fields are hardcoded constants, not user input
        field_list = ", ".join(fields)
        return f"SELECT {field_list} FROM Opportunity"  # noqa: S608  # nosec B608

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
        attempts_remaining = 5  # More attempts since we have relationship fields

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
                    raise

                if missing_field in fields:
                    fields = [f for f in fields if f != missing_field]
                    continue

                raise
