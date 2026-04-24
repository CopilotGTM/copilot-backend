"""Salesforce fetcher for CRM task record sync."""

from __future__ import annotations

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
_MAX_PAGES = 500


class SalesforceTasksFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(self) -> list[dict[str, Any]]:
        soql = (
            "SELECT Id, Subject, Description, Status, Priority, ActivityDate, "
            "CompletedDateTime, OwnerId, WhoId, WhatId, AccountId, CreatedDate, "
            "LastModifiedDate, IsDeleted "
            "FROM Task WHERE IsDeleted = false ORDER BY LastModifiedDate, Id"
        )
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        records: list[dict[str, Any]] = []
        next_url: str | None = None
        page_count = 0

        while page_count < _MAX_PAGES:
            payload = await self._get_json(
                next_url or url, params=None if next_url else {"q": soql}
            )
            raw_records = payload.get("records")
            if not isinstance(raw_records, list):
                raise ValueError("Salesforce tasks query API returned non-list records")
            records.extend(row for row in raw_records if isinstance(row, dict))

            page_count += 1
            next_records_url = payload.get("nextRecordsUrl")
            if not isinstance(next_records_url, str) or not next_records_url:
                break
            next_url = (
                next_records_url
                if next_records_url.startswith("http")
                else f"{self.instance_url}{next_records_url}"
            )
        else:
            logger.warning(
                "Salesforce tasks query hit max page ceiling",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
                },
            )

        logger.info(
            "Fetched Salesforce tasks",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": len(records),
                "pages": page_count,
            },
        )
        return records

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
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                response.raise_for_status()
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce tasks API returned non-dict payload")
        return payload
