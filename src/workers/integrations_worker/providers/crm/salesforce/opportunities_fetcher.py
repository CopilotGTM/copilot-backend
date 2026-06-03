"""Salesforce fetcher for Opportunity fact sync."""

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
_EXCLUDED_FIELD_TYPES = {"address", "location", "base64"}
_CANONICAL_FIELDS = {
    "Id",
    "Name",
    "Amount",
    "CurrencyIsoCode",
    "CloseDate",
    "StageName",
    "RecordTypeId",
    "OwnerId",
    "AccountId",
    "CreatedDate",
    "LastModifiedDate",
    "CreatedById",
    "LastModifiedById",
    "Description",
    "Type",
    "ForecastCategoryName",
    "Probability",
    "ExpectedRevenue",
    "LeadSource",
    "LastActivityDate",
    "DaysToClose",
    "IsClosed",
    "IsWon",
}
_REQUIRED_FIELDS = {"Id"}


class SalesforceOpportunitiesFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(self) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        field_names: list[str] = []
        async for page_records, page_field_names in self.fetch_pages():
            records.extend(page_records)
            if not field_names:
                field_names = page_field_names
        return records, field_names

    async def fetch_pages(self) -> AsyncIterator[tuple[list[dict[str, Any]], list[str]]]:
        field_names = await self._fetch_field_names()
        soql = (
            f"SELECT {', '.join(field_names)} FROM Opportunity "  # noqa: S608  # nosec B608
            f"ORDER BY {_build_order_by(field_names)}"
        )
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
                raise ValueError("Salesforce opportunity query API returned non-list records")
            page_records = [row for row in raw_records if isinstance(row, dict)]
            total_records += len(page_records)

            page_count += 1
            next_records_url = payload.get("nextRecordsUrl")
            yield page_records, field_names
            if not isinstance(next_records_url, str) or not next_records_url:
                break
            next_url = (
                next_records_url
                if next_records_url.startswith("http")
                else f"{self.instance_url}{next_records_url}"
            )
        else:
            logger.warning(
                "Salesforce opportunities query hit max page ceiling",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
                },
            )

        logger.info(
            "Fetched Salesforce opportunities",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": total_records,
                "pages": page_count,
                "field_count": len(field_names),
            },
        )

    async def _fetch_field_names(self) -> list[str]:
        url = f"{self.instance_url}/services/data/{_API_VERSION}/sobjects/Opportunity/describe"
        payload = await self._get_json(url)
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, list):
            raise ValueError("Salesforce Opportunity describe API returned non-list fields")

        field_names = {
            name
            for field in raw_fields
            if isinstance(field, dict)
            for name in [_extract_field_name(field)]
            if name is not None
        }
        field_names.update(_REQUIRED_FIELDS)

        missing_canonical_fields = sorted(_CANONICAL_FIELDS - field_names)
        if missing_canonical_fields:
            logger.warning(
                "Salesforce Opportunity describe omitted canonical fields",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "missing_fields": missing_canonical_fields,
                },
            )

        return sorted(field_names)

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
            raise ValueError("Salesforce opportunities API returned non-dict payload")
        return payload


def _extract_field_name(field: dict[str, Any]) -> str | None:
    name = field.get("name")
    if not isinstance(name, str) or not name:
        return None
    if field.get("queryable") is False:
        return None
    if field.get("deprecatedAndHidden") is True:
        return None
    field_type = field.get("type")
    if isinstance(field_type, str) and field_type.lower() in _EXCLUDED_FIELD_TYPES:
        return None
    return name


def _build_order_by(field_names: list[str]) -> str:
    available_fields = set(field_names)
    order_fields: list[str] = []
    if "CloseDate" in available_fields:
        order_fields.append("CloseDate DESC")
    if "Name" in available_fields:
        order_fields.append("Name")
    order_fields.append("Id")
    return ", ".join(order_fields)


def _raise_for_status_with_salesforce_body(response: httpx.Response) -> None:
    if not response.is_error:
        return

    body = response.text[:_MAX_ERROR_BODY_CHARS]
    message = (
        f"{response.status_code} {response.reason_phrase} for url '{response.url}'. "
        f"Salesforce response body: {body}"
    )
    raise httpx.HTTPStatusError(message, request=response.request, response=response)
