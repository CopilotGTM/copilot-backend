"""HubSpot deal pipelines fetcher.

GET /crm/v3/pipelines/deals returns every active pipeline for the portal,
including each pipeline's stages inline. The endpoint is not paginated —
HubSpot caps deal pipelines per portal — so this fetcher issues a single
request and returns the raw `results` array.

No classification, no normalization, no DB. Just HTTP with token retry.
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

_BASE_URL = "https://api.hubapi.com/crm/v3/pipelines"
_DEFAULT_TIMEOUT = 30.0


class HubSpotPipelinesFetcher:
    """Fetch deal pipelines (with stages) from HubSpot's Pipelines API."""

    def __init__(self, *, token_info: IntegrationTokenInfo) -> None:
        self.token_info = token_info

    async def fetch_deal_pipelines(self) -> list[dict[str, Any]]:
        """Return the raw `results` list — one dict per pipeline, stages inline.

        Raises ValueError if the response shape is malformed (missing `results`
        or `results` not a list). 401/429/5xx retries are handled by
        `call_with_token_retry` and the caller's Celery retry policy.
        """
        url = f"{_BASE_URL}/deals"

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
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("HubSpot deal pipelines API returned non-list `results`")
        logger.info(
            "Fetched HubSpot deal pipelines",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "pipeline_count": len(results),
            },
        )
        return results
