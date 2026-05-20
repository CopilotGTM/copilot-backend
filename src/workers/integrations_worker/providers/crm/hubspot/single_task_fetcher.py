"""Fetch one full HubSpot task for incremental upserts."""

from __future__ import annotations

from typing import Any

import httpx

from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers.crm.hubspot.tasks_fetcher import (
    HUBSPOT_TASK_PROPERTIES,
)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/tasks/{task_id}"
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_ASSOCIATIONS: tuple[str, ...] = ("deals", "contacts", "companies")


class HubSpotSingleTaskFetcher:
    """Fetch the full HubSpot task payload needed by existing task writers."""

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        task_id: str,
        properties: tuple[str, ...] = HUBSPOT_TASK_PROPERTIES,
        associations: tuple[str, ...] = _DEFAULT_ASSOCIATIONS,
    ) -> None:
        if not properties:
            raise ValueError("HubSpotSingleTaskFetcher requires a non-empty properties list")
        self.token_info = token_info
        self.task_id = task_id
        self._properties = tuple(sorted(set(properties)))
        self._associations = tuple(sorted(set(associations)))

    async def fetch(self) -> dict[str, Any] | None:
        """Return the current HubSpot task payload, or None on HubSpot 404."""
        response = await call_with_token_retry(
            self.token_info,
            _make_request(
                self.task_id,
                {
                    "archived": "false",
                    "properties": ",".join(self._properties),
                    "associations": ",".join(self._associations),
                },
            ),
        )
        if response.status_code == 404:
            return None

        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HubSpot single task API returned a non-object payload")
        return payload


def _make_request(task_id: str, params: dict[str, str]):
    """Build the retryable authenticated request closure for token refresh logic."""

    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.get(
                _BASE_URL.format(task_id=task_id),
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if response.status_code != 404:
                response.raise_for_status()
            return response

    return _request


__all__ = ["HubSpotSingleTaskFetcher"]
