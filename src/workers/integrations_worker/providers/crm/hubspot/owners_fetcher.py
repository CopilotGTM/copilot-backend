"""HubSpot owners fetcher.

GET /crm/v3/owners?archived=false paginated via `paging.next.after` cursor.

No classification, no normalisation, no DB. Pure HTTP + pagination + token
refresh. Raises on malformed responses; retries / auth recovery are handled
by `call_with_token_retry` and the caller's Celery retry policy.
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

_BASE_URL = "https://api.hubapi.com/crm/v3/owners"
_DEFAULT_TIMEOUT = 30.0
_PAGE_LIMIT = 100
# HubSpot's docs say owners queries are cheap but a pathological portal with
# runaway owner counts shouldn't hang the sync forever.
_MAX_PAGES = 200


class HubSpotOwnersFetcher:
    """Fetch all (non-archived) HubSpot owners for the integration."""

    def __init__(self, *, token_info: IntegrationTokenInfo) -> None:
        self.token_info = token_info

    async def fetch_all(self) -> list[dict[str, Any]]:
        """Return every owner as a raw dict. Archived owners are filtered server-side."""
        all_owners: list[dict[str, Any]] = []
        after: str | None = None

        for page_no in range(_MAX_PAGES):
            params: dict[str, str] = {
                "archived": "false",
                "limit": str(_PAGE_LIMIT),
            }
            if after is not None:
                params["after"] = after

            response = await call_with_token_retry(
                self.token_info,
                _make_request(params),
            )
            payload = response.json()
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("HubSpot owners API returned non-list `results`")
            all_owners.extend(results)

            next_cursor = _extract_next_cursor(payload)
            if next_cursor is None:
                break
            after = next_cursor

            if page_no + 1 >= _MAX_PAGES:
                logger.warning(
                    "HubSpot owners fetch hit _MAX_PAGES ceiling; stopping",
                    extra={
                        "integration_id": str(self.token_info.integration_id),
                        "page_limit": _PAGE_LIMIT,
                        "max_pages": _MAX_PAGES,
                        "collected": len(all_owners),
                    },
                )

        logger.info(
            "Fetched HubSpot owners",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "owner_count": len(all_owners),
            },
        )
        return all_owners


def _make_request(params: dict[str, str]):
    """Build a token-accepting async request fn bound to this page's params.

    Explicit factory to avoid closing over a loop variable (ruff B023).
    """

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
    """Return `payload.paging.next.after` if present, else None."""
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
