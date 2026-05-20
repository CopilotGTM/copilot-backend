"""Fetch one full HubSpot contact for webhook and incremental upserts."""

from __future__ import annotations

from typing import Any

import httpx

from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers.crm.hubspot.contacts_fetcher import (
    HUBSPOT_CONTACT_PROPERTIES,
)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
_DEFAULT_TIMEOUT = 60.0


class HubSpotSingleContactFetcher:
    """Fetch the full HubSpot contact payload needed by existing contact writers."""

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        contact_id: str,
        properties: tuple[str, ...] = HUBSPOT_CONTACT_PROPERTIES,
    ) -> None:
        if not properties:
            raise ValueError("HubSpotSingleContactFetcher requires a non-empty properties list")
        self.token_info = token_info
        self.contact_id = contact_id
        self._properties = tuple(sorted(set(properties)))

    async def fetch(self) -> dict[str, Any] | None:
        """Return the current HubSpot contact payload, or None on HubSpot 404."""
        response = await call_with_token_retry(
            self.token_info,
            _make_request(
                self.contact_id,
                {
                    "archived": "false",
                    "properties": ",".join(self._properties),
                },
            ),
        )
        if response.status_code == 404:
            return None

        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HubSpot single contact API returned a non-object payload")
        return payload


def _make_request(contact_id: str, params: dict[str, str]):
    """Build the retryable authenticated request closure for token refresh logic."""

    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.get(
                _BASE_URL.format(contact_id=contact_id),
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if response.status_code != 404:
                response.raise_for_status()
            return response

    return _request
