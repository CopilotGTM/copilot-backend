"""Batch-read HubSpot CRM object ids for delete reconciliation.

This fetcher intentionally requests only `hs_object_id`. Reconciliation only
needs to prove that local active ids still exist in HubSpot's active object set;
full object snapshots stay owned by webhook/incremental upsert paths.
"""

from __future__ import annotations

import httpx

from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/{object_type}/batch/read"
_DEFAULT_TIMEOUT = 60.0
_SUPPORTED_OBJECT_TYPES = frozenset({"deals", "companies", "contacts", "tasks", "notes"})


class HubSpotBatchObjectExistenceFetcher:
    """Return the subset of requested HubSpot ids that still exist as active objects."""

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        object_type: str,
        object_ids: list[str],
    ) -> None:
        if object_type not in _SUPPORTED_OBJECT_TYPES:
            raise ValueError(f"Unsupported HubSpot reconciliation object: {object_type}")
        self.token_info = token_info
        self.object_type = object_type
        self.object_ids = list(dict.fromkeys(object_ids))

    async def fetch_existing_ids(self) -> set[str]:
        """Return only ids that still resolve in HubSpot's active object set."""
        if not self.object_ids:
            return set()

        response = await call_with_token_retry(
            self.token_info,
            _make_request(self.object_type, self.object_ids),
        )
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise ValueError("HubSpot batch object read returned non-list `results`")

        existing_ids: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                raise ValueError("HubSpot batch object read returned malformed result")
            object_id = item.get("id")
            if object_id is not None:
                existing_ids.add(str(object_id))
        return existing_ids


def _make_request(object_type: str, object_ids: list[str]):
    """Build the retryable authenticated request closure for token refresh logic."""

    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.post(
                _BASE_URL.format(object_type=object_type),
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "archived": False,
                    "properties": ["hs_object_id"],
                    "inputs": [{"id": object_id} for object_id in object_ids],
                },
            )
            response.raise_for_status()
            return response

    return _request


__all__ = ["HubSpotBatchObjectExistenceFetcher"]
