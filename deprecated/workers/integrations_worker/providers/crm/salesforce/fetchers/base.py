"""Shared Salesforce fetcher utilities.

Salesforce is accessed via Nango proxy, which forwards requests to the provider API
and handles auth/refresh.

This module implements SOQL query pagination (records + nextRecordsUrl).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.core import BaseFetcher

logger = get_logger(__name__)


def _format_soql_datetime(value: datetime) -> str:
    """Format a datetime for SOQL (UTC, no micros).

    Salesforce accepts ISO-8601 datetimes like: 2025-12-17T10:15:30Z
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


class SalesforceSOQLFetcher(BaseFetcher):
    """Base fetcher for Salesforce objects via SOQL."""

    api_version: str = "v59.0"

    async def _query_all(self, soql: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        endpoint = f"/services/data/{self.api_version}/query"
        params: dict[str, Any] | None = {"q": soql}

        while True:
            response = await self.nango_client.proxy_request(
                provider_config_key=self.provider_config_key,
                connection_id=self.connection_id,
                endpoint=endpoint,
                method="GET",
                params=params,
            )

            # Log response metadata for debugging
            total_size = response.get("totalSize")
            done = response.get("done")
            logger.debug(
                "SOQL response: totalSize=%s done=%s endpoint=%s",
                total_size,
                done,
                endpoint,
            )

            page_records = response.get("records")
            if isinstance(page_records, list):
                records.extend([r for r in page_records if isinstance(r, dict)])
                logger.debug("Extracted %d records from page", len(page_records))

            next_url = response.get("nextRecordsUrl")

            if done is True or not next_url:
                break

            # Subsequent pages use nextRecordsUrl directly.
            endpoint = str(next_url)
            params = None

        logger.info("SOQL query completed: total_records=%d query=%s", len(records), soql[:100])
        return records

    @staticmethod
    def _add_incremental_where(
        base_query: str,
        *,
        last_synced_at: datetime | None,
    ) -> str:
        if not last_synced_at:
            return base_query

        stamp = _format_soql_datetime(last_synced_at)
        if " where " in base_query.lower():
            return f"{base_query} AND LastModifiedDate >= {stamp}"
        return f"{base_query} WHERE LastModifiedDate >= {stamp}"
