"""Salesforce fetcher for CRM user record sync."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

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


class SalesforceUsersFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_all(self) -> list[dict[str, Any]]:
        soql = f"{_USER_SELECT_SOQL} ORDER BY Name"
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
                raise ValueError("Salesforce users query API returned non-list records")
            records.extend(row for row in raw_records if isinstance(row, dict))

            page_count += 1
            next_records_url = payload.get("nextRecordsUrl")
            if not isinstance(next_records_url, str) or not next_records_url:
                break
            next_url = _resolve_next_records_url(
                instance_url=self.instance_url,
                next_records_url=next_records_url,
            )
        else:
            logger.warning(
                "Salesforce users query hit max page ceiling",
                extra={
                    "integration_id": str(self.token_info.integration_id),
                    "max_pages": _MAX_PAGES,
                },
            )

        logger.info(
            "Fetched Salesforce users",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "count": len(records),
                "pages": page_count,
            },
        )
        return records

    async def fetch_one(self, user_id: str) -> dict[str, Any] | None:
        escaped_user_id = _escape_soql_literal(user_id)
        soql = f"{_USER_SELECT_SOQL} WHERE Id = '{escaped_user_id}' LIMIT 1"
        url = f"{self.instance_url}/services/data/{_API_VERSION}/query"
        payload = await self._get_json(url, params={"q": soql})
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("Salesforce users query API returned non-list records")
        for row in raw_records:
            if isinstance(row, dict):
                return row
        return None

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
            raise ValueError("Salesforce users API returned non-dict payload")
        return payload


_USER_SELECT_SOQL = (
    "SELECT Id, Email, FirstName, LastName, Name, IsActive, UserType, ProfileId, "
    "Profile.Name, LastModifiedDate FROM User"
)


def _escape_soql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _resolve_next_records_url(*, instance_url: str, next_records_url: str) -> str:
    if next_records_url.startswith("/services/data/"):
        return f"{instance_url}{next_records_url}"

    parsed_next = urlparse(next_records_url)
    parsed_instance = urlparse(instance_url)
    if (
        parsed_next.scheme == "https"
        and parsed_next.netloc.lower() == parsed_instance.netloc.lower()
        and parsed_next.path.startswith("/services/data/")
        and not parsed_next.username
        and not parsed_next.password
    ):
        return next_records_url

    raise ValueError("Salesforce users API returned unsupported nextRecordsUrl")
