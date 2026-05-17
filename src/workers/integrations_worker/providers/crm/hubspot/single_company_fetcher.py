"""Fetch one full HubSpot company for webhook upserts.

Company webhooks carry only the portal/object identity, so the worker fetches
the current company snapshot and passes it through the same normalizer and
writer code used by full company sync.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers.crm.hubspot.companies_fetcher import (
    HUBSPOT_COMPANY_PROPERTIES,
)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/companies/{company_id}"
_DEFAULT_TIMEOUT = 60.0


class HubSpotSingleCompanyFetcher:
    """Fetch the full HubSpot company payload needed by existing company writers."""

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        company_id: str,
        properties: tuple[str, ...] = HUBSPOT_COMPANY_PROPERTIES,
    ) -> None:
        if not properties:
            raise ValueError("HubSpotSingleCompanyFetcher requires a non-empty properties list")
        self.token_info = token_info
        self.company_id = company_id
        self._properties = tuple(sorted(set(properties)))

    async def fetch(self) -> dict[str, Any] | None:
        """Return the current HubSpot company payload, or None on HubSpot 404."""
        response = await call_with_token_retry(
            self.token_info,
            _make_request(
                self.company_id,
                {
                    "archived": "false",
                    "properties": ",".join(self._properties),
                },
            ),
        )
        if response.status_code == 404:
            # HubSpot may deliver an update after archive/delete. The handler
            # translates this into a local tombstone instead of retrying forever.
            return None

        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HubSpot single company API returned a non-object payload")
        return payload


def _make_request(company_id: str, params: dict[str, str]):
    """Build the retryable authenticated request closure for token refresh logic."""

    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.get(
                _BASE_URL.format(company_id=company_id),
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if response.status_code != 404:
                response.raise_for_status()
            return response

    return _request
