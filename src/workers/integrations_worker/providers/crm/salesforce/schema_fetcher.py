"""Salesforce describe API fetcher for schema sync."""

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


class SalesforceSchemaFetcher:
    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
    ) -> None:
        self.token_info = token_info
        self.instance_url = instance_url.rstrip("/")

    async def fetch_fields(self, *, object_type: str) -> list[dict[str, Any]]:
        url = f"{self.instance_url}/services/data/{_API_VERSION}/sobjects/{object_type}/describe"

        async def _request(access_token: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return response

        response = await call_with_token_retry(self.token_info, _request)
        payload = response.json()
        fields = payload.get("fields")
        if not isinstance(fields, list):
            raise ValueError(f"Salesforce {object_type} describe API returned non-list fields")
        logger.info(
            "Fetched Salesforce schema",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "object_type": object_type,
                "count": len(fields),
            },
        )
        return fields
