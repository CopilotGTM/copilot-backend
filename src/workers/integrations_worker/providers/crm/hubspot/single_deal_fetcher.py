"""Fetch one full HubSpot deal for webhook upserts.

Webhook events only identify the changed object, so the deal handler uses this
fetcher to retrieve the same payload shape consumed by the normal paginated
deal sync pipeline.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/deals/{deal_id}"
_DEFAULT_TIMEOUT = 60.0
# These associations mirror the paginated deal fetcher so webhook upserts do
# not silently lose relationship data compared with full sync.
_DEFAULT_ASSOCIATIONS: tuple[str, ...] = ("contacts", "companies", "line_items")


class HubSpotSingleDealFetcher:
    """Fetch the full HubSpot deal payload needed by the existing deal writers."""

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        deal_id: str,
        properties: list[str],
        history_properties: tuple[str, ...] | None = None,
        associations: tuple[str, ...] = _DEFAULT_ASSOCIATIONS,
    ) -> None:
        if not properties:
            raise ValueError("HubSpotSingleDealFetcher requires a non-empty properties list")
        self.token_info = token_info
        self.deal_id = deal_id
        self._properties = sorted(set(properties))
        self._history_properties = (
            tuple(self._properties) if history_properties is None else history_properties
        )
        self._associations = associations

    async def fetch(self) -> dict[str, Any] | None:
        """Return the current HubSpot deal payload, or None when HubSpot returns 404."""
        params: dict[str, str] = {
            "archived": "false",
            "properties": ",".join(self._properties),
        }
        if self._history_properties:
            params["propertiesWithHistory"] = ",".join(self._history_properties)
        if self._associations:
            params["associations"] = ",".join(self._associations)

        response = await call_with_token_retry(
            self.token_info,
            _make_request(self.deal_id, params),
        )
        if response.status_code == 404:
            # A 404 means the object is gone or inaccessible after the webhook
            # was sent; callers translate that into a local soft-delete.
            return None

        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HubSpot single deal API returned a non-object payload")
        return payload


def _make_request(deal_id: str, params: dict[str, str]):
    """Build the retryable authenticated request closure for token refresh logic."""

    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.get(
                _BASE_URL.format(deal_id=deal_id),
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if response.status_code != 404:
                response.raise_for_status()
            return response

    return _request
