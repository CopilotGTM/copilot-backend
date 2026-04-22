"""HubSpot Properties API fetcher.

Fetches raw property definitions for a CRM object type (deals today; contacts
and companies later). No classification, no DB — just HTTP with token refresh
and archived-filtering.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

logger = get_logger(__name__)

_BASE_URL = "https://api.hubapi.com/crm/v3/properties"
_DEFAULT_TIMEOUT = 30.0


class HubSpotSchemaFetcher:
    """Fetch HubSpot schema metadata using OAuth token retry helpers."""

    def __init__(self, *, token_info: IntegrationTokenInfo) -> None:
        self.token_info = token_info

    async def fetch_properties(self, *, object_type: str) -> list[dict[str, Any]]:
        """Fetch active fields for a HubSpot object type."""

        url = f"{_BASE_URL}/{object_type}"

        async def _request(access_token: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"archived": "false"},
                )
                response.raise_for_status()
                return response

        response = await call_with_token_retry(self.token_info, _request)
        # Properties API can evolve shape over time; validate defensively before
        # passing into classifier logic.
        payload = response.json()
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError(f"HubSpot {object_type} properties API returned non-list results")
        logger.info(
            "Fetched HubSpot schema",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "object_type": object_type,
                "count": len(results),
            },
        )
        return results
