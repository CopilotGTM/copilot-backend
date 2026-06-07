"""Salesforce fetcher for CRM task record sync."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, cast

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 60.0
_API_VERSION = "v61.0"
_MAX_PAGES = 500_000
_MAX_REQUEST_ATTEMPTS = 3
_REQUEST_RETRY_DELAYS = (1.0, 3.0)
_TASK_DEAL_SCOPE_BATCH_SIZE = 200


class SalesforceTasksFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(
        self,
        *,
        deal_external_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        async for batch in self.iter_batches(deal_external_ids=deal_external_ids):
            records.extend(batch)
        return records

    async def iter_batches(
        self,
        *,
        deal_external_ids: list[str] | None = None,
    ):
        if deal_external_ids is None:
            async for batch in self._iter_query_batches(_task_query_soql()):
                yield batch
            return

        scoped_ids = [deal_id for deal_id in deal_external_ids if deal_id]
        if not scoped_ids:
            return

        for scoped_batch in _chunked(scoped_ids, _TASK_DEAL_SCOPE_BATCH_SIZE):
            async for batch in self._iter_query_batches(_task_query_soql(scoped_batch)):
                yield batch

    async def _iter_query_batches(self, soql: str):
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        next_url: str | None = None
        page_count = 0
        total_records = 0

        while page_count < _MAX_PAGES:
            payload = await self._get_json(
                next_url or url, params=None if next_url else {"q": soql}
            )
            raw_records = payload.get("records")
            if not isinstance(raw_records, list):
                raise ValueError("Salesforce tasks query API returned non-list records")
            batch = [row for row in raw_records if isinstance(row, dict)]
            total_records += len(batch)
            if batch:
                yield batch

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
                "count": total_records,
                "pages": page_count,
            },
        )

    async def fetch_one(self, task_id: str) -> dict[str, Any] | None:
        soql = (
            "SELECT Id, Subject, Description, Status, Priority, ActivityDate, "  # noqa: S608  # nosec B608
            "CompletedDateTime, OwnerId, WhoId, WhatId, AccountId, CreatedDate, "
            "LastModifiedDate, IsDeleted "
            f"FROM Task WHERE Id = '{_escape_soql_literal(task_id)}' LIMIT 1"
        )
        rows = await self._query_all(soql, error_label="tasks single")
        return rows[0] if rows else None

    async def _query_all(self, soql: str, *, error_label: str) -> list[dict[str, Any]]:
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
                raise ValueError(f"Salesforce {error_label} query API returned non-list records")
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
                "Salesforce %s query hit max page ceiling",
                error_label,
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
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

        response = await _call_with_transient_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce tasks API returned non-dict payload")
        return payload


async def _call_with_transient_retry(
    token_info: IntegrationTokenInfo,
    request_fn,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(_MAX_REQUEST_ATTEMPTS):
        try:
            return cast(httpx.Response, await call_with_token_retry(token_info, request_fn))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {429, 500, 502, 503, 504}:
                raise
            last_error = exc

        if attempt < len(_REQUEST_RETRY_DELAYS):
            await asyncio.sleep(_REQUEST_RETRY_DELAYS[attempt])

    if last_error is None:
        raise RuntimeError("Salesforce task request retry failed without an error")
    raise last_error


def _escape_soql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _task_query_soql(deal_external_ids: list[str] | None = None) -> str:
    where_clause = "WHERE IsDeleted = false"
    if deal_external_ids is not None:
        where_clause += f" AND WhatId IN ({_quoted_csv(deal_external_ids)})"
    return (
        "SELECT Id, Subject, Description, Status, Priority, ActivityDate, "  # noqa: S608  # nosec B608
        "CompletedDateTime, OwnerId, WhoId, WhatId, AccountId, CreatedDate, "
        "LastModifiedDate, IsDeleted "
        f"FROM Task {where_clause} ORDER BY LastModifiedDate, Id"
    )


def _chunked(values: Iterable[str], size: int) -> Iterable[list[str]]:
    chunk: list[str] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _quoted_csv(values: list[str]) -> str:
    return ", ".join("'" + _escape_soql_literal(value) + "'" for value in values)
