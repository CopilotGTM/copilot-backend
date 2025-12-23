"""Salesforce Note fetcher.

Fetches legacy Salesforce Note records. These are typically attached to a parent
record via ParentId (e.g., Opportunity).

We convert these notes into our canonical activity payload shape in the provider
wrapper so they can be stored in crm_activities.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.service.integrations.nango_client import NangoConnectionError

from .base import SalesforceSOQLFetcher


class SalesforceNoteFetcher(SalesforceSOQLFetcher):
    """Fetcher for Salesforce Note records."""

    _REQUIRED_FIELDS: tuple[str, ...] = (
        "Id",
        "Title",
        "Body",
        "OwnerId",
        "ParentId",
        "CreatedDate",
        "LastModifiedDate",
        "IsDeleted",
    )

    _OWNER_FIELDS: tuple[str, ...] = (
        "Owner.Id",
        "Owner.Name",
        "Owner.Email",
        "Owner.FirstName",
        "Owner.LastName",
    )

    @staticmethod
    def _build_query(fields: list[str]) -> str:
        field_list = ", ".join(fields)
        return f"SELECT {field_list} FROM Note"  # noqa: S608  # nosec B608

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
        fields = [*self._REQUIRED_FIELDS, *self._OWNER_FIELDS]
        attempts_remaining = 5

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

                if missing_field in self._REQUIRED_FIELDS:
                    raise

                if missing_field in fields:
                    fields = [f for f in fields if f != missing_field]
                    continue

                raise
