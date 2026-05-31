"""Salesforce fetcher for Contact record sync."""

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
_MAX_ERROR_BODY_CHARS = 500
_REQUIRED_FIELDS = {"Id", "CreatedDate", "LastModifiedDate"}
_EXCLUDED_FIELD_TYPES = {"address", "location", "base64"}


class SalesforceContactsFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(self) -> list[dict[str, Any]]:
        field_names = await self._fetch_field_names()
        # The field list comes only from Salesforce describe metadata after
        # filtering out known non-selectable field shapes; no user input reaches this SOQL.
        soql = f"SELECT {', '.join(field_names)} FROM Contact ORDER BY LastName, FirstName"  # noqa: S608  # nosec B608
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
                raise ValueError("Salesforce contact query API returned non-list records")
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
                "Salesforce contacts query hit max page ceiling",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
                },
            )

        logger.info(
            "Fetched Salesforce contacts",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": len(records),
                "pages": page_count,
                "field_count": len(field_names),
            },
        )
        return records

    async def fetch_one(self, contact_id: str) -> dict[str, Any] | None:
        field_names = await self._fetch_field_names()
        soql = (
            f"SELECT {', '.join(field_names)} FROM Contact "  # noqa: S608  # nosec B608
            f"WHERE Id = '{_escape_soql_literal(contact_id)}' LIMIT 1"
        )
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        payload = await self._get_json(url, params={"q": soql})
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("Salesforce single Contact query API returned non-list records")
        if not raw_records:
            return None
        record = raw_records[0]
        if not isinstance(record, dict):
            raise ValueError("Salesforce single Contact query API returned non-dict record")
        return record

    async def _fetch_field_names(self) -> list[str]:
        url = f"{self.instance_url}/services/data/{_API_VERSION}/sobjects/Contact/describe"
        payload = await self._get_json(url)
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, list):
            raise ValueError("Salesforce Contact describe API returned non-list fields")

        field_names = {
            name
            for field in raw_fields
            if isinstance(field, dict)
            for name in [_extract_field_name(field)]
            if name is not None
        }
        field_names.update(_REQUIRED_FIELDS)
        if "Id" not in field_names:
            raise ValueError("Salesforce Contact describe API did not yield a usable Id field")
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
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                _raise_for_status_with_salesforce_body(response)
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Salesforce contacts API returned non-dict payload")
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


def _escape_soql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _raise_for_status_with_salesforce_body(response: httpx.Response) -> None:
    if not response.is_error:
        return

    body = response.text[:_MAX_ERROR_BODY_CHARS]
    message = (
        f"{response.status_code} {response.reason_phrase} for url '{response.url}'. "
        f"Salesforce response body: {body}"
    )
    raise httpx.HTTPStatusError(message, request=response.request, response=response)
