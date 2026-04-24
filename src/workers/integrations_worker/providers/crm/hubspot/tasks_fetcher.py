"""HubSpot tasks fetcher.

Fetches first-class HubSpot task objects via `/crm/v3/objects/tasks`.
Pure HTTP + pagination + token retry; no DB or normalization.
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

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/tasks"
_DEFAULT_TIMEOUT = 30.0
_PAGE_LIMIT = 100
_MAX_PAGES = 2_000
_DEFAULT_PROPERTIES: tuple[str, ...] = (
    "hs_task_subject",
    "hs_task_body",
    "hs_task_status",
    "hs_timestamp",
    "hubspot_owner_id",
    "hs_createdate",
    "hs_lastmodifieddate",
)
_DEFAULT_ASSOCIATIONS: tuple[str, ...] = ("deals", "contacts", "companies")


class HubSpotTasksFetcher:
    """Fetch all non-archived HubSpot tasks for one integration."""

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        properties: tuple[str, ...] = _DEFAULT_PROPERTIES,
        associations: tuple[str, ...] = _DEFAULT_ASSOCIATIONS,
    ) -> None:
        self.token_info = token_info
        self._properties = tuple(sorted(set(properties)))
        self._associations = tuple(sorted(set(associations)))

    async def fetch_all(self) -> list[dict[str, Any]]:
        all_tasks: list[dict[str, Any]] = []
        after: str | None = None

        for page_no in range(_MAX_PAGES):
            params: dict[str, str] = {
                "archived": "false",
                "limit": str(_PAGE_LIMIT),
                "properties": ",".join(self._properties),
                "associations": ",".join(self._associations),
            }
            if after is not None:
                params["after"] = after

            response = await call_with_token_retry(self.token_info, _make_request(params))
            payload = response.json()
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("HubSpot tasks API returned non-list `results`")
            all_tasks.extend(results)

            next_cursor = _extract_next_cursor(payload)
            if next_cursor is None:
                break
            after = next_cursor

            if page_no + 1 >= _MAX_PAGES:
                logger.warning(
                    "HubSpot tasks fetch hit _MAX_PAGES ceiling; stopping",
                    extra={
                        "integration_id": str(self.token_info.integration_id),
                        "collected": len(all_tasks),
                    },
                )

        logger.info(
            "Fetched HubSpot tasks",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "task_count": len(all_tasks),
            },
        )
        return all_tasks


def _make_request(params: dict[str, str]):
    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.get(
                _BASE_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            response.raise_for_status()
            return response

    return _request


def _extract_next_cursor(payload: dict[str, Any]) -> str | None:
    paging = payload.get("paging")
    if not isinstance(paging, dict):
        return None
    nxt = paging.get("next")
    if not isinstance(nxt, dict):
        return None
    after = nxt.get("after")
    if isinstance(after, str) and after:
        return after
    return None
