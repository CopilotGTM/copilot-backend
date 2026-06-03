"""Salesforce fetcher for OpportunityHistory stage-change rows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 60.0
_API_VERSION = "v61.0"
_MAX_PAGES = 1_500_000
_QUERY_BATCH_SIZE = 2_000
_MAX_ERROR_BODY_CHARS = 500


class SalesforceOpportunityHistoryFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        async for page_records in self.fetch_pages():
            records.extend(page_records)
        return records

    async def fetch_pages(self) -> AsyncIterator[list[dict[str, Any]]]:
        soql = (
            "SELECT OpportunityId, StageName, Amount, Probability, CreatedDate "
            "FROM OpportunityHistory ORDER BY OpportunityId, CreatedDate"
        )
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        next_url: str | None = None
        page_count = 0
        total_records = 0

        while page_count < _MAX_PAGES:
            payload = await self._get_json(
                next_url or url, params=None if next_url else {"q": soql}
            )
            page_records = payload.get("records")
            if not isinstance(page_records, list):
                raise ValueError(
                    "Salesforce OpportunityHistory query API returned non-list records"
                )
            valid_records = [row for row in page_records if isinstance(row, dict)]
            total_records += len(valid_records)

            page_count += 1
            next_records_url = payload.get("nextRecordsUrl")
            yield valid_records
            if not isinstance(next_records_url, str) or not next_records_url:
                break
            next_url = (
                next_records_url
                if next_records_url.startswith("http")
                else f"{self.instance_url}{next_records_url}"
            )
        else:
            logger.warning(
                "Salesforce OpportunityHistory query hit max page ceiling",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
                },
            )

        logger.info(
            "Fetched Salesforce opportunity history rows",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": total_records,
                "pages": page_count,
            },
        )

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async def _request(access_token: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Sforce-Query-Options": f"batchSize={_QUERY_BATCH_SIZE}",
                    },
                    params=params,
                )
                _raise_for_status_with_salesforce_body(response)
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce OpportunityHistory API returned non-dict payload")
        return payload


def _raise_for_status_with_salesforce_body(response: httpx.Response) -> None:
    if not response.is_error:
        return

    body = response.text[:_MAX_ERROR_BODY_CHARS]
    message = (
        f"{response.status_code} {response.reason_phrase} for url '{response.url}'. "
        f"Salesforce response body: {body}"
    )
    raise httpx.HTTPStatusError(message, request=response.request, response=response)
