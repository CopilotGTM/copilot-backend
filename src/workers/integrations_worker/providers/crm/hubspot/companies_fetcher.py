"""HubSpot companies fetcher.

GET /crm/v3/objects/companies?archived=false paginated via `paging.next.after`.

No classification, no normalisation, no DB. Pure HTTP + pagination + token
refresh. Raises on malformed responses; retries / auth recovery are handled
by `call_with_token_retry` and the caller's Celery retry policy.

Property list is a curated static set — custom-property discovery is deferred
to a later phase, analogous to the existing deal schema sync.
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

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/companies"
_DEFAULT_TIMEOUT = 30.0
_PAGE_LIMIT = 100
# Up to 500k companies per run; far beyond realistic portal counts but bounds
# runaway pagination. Hitting the ceiling triggers a WARN.
_MAX_PAGES = 5000

# Hardcoded curated property list — every column CRMCompany materialises,
# plus provider-native timestamps for fallbacks. Extend this list (or switch
# to schema-driven discovery) when adding new columns.
HUBSPOT_COMPANY_PROPERTIES: tuple[str, ...] = (
    "name",
    "domain",
    "website",
    "description",
    "industry",
    "type",
    "lifecyclestage",
    "hubspot_owner_id",
    "numberofemployees",
    "annualrevenue",
    "phone",
    "address",
    "city",
    "state",
    "country",
    "zip",
    "timezone",
    "linkedin_company_page",
    "hs_createdate",
    "hs_lastmodifieddate",
)


class HubSpotCompaniesFetcher:
    """Fetch all (non-archived) HubSpot companies for the integration."""

    def __init__(self, *, token_info: IntegrationTokenInfo) -> None:
        self.token_info = token_info

    async def fetch_all(self) -> list[dict[str, Any]]:
        """Return every company as a raw dict. Archived records filtered server-side."""
        all_companies: list[dict[str, Any]] = []
        after: str | None = None
        properties_param = ",".join(HUBSPOT_COMPANY_PROPERTIES)

        for page_no in range(_MAX_PAGES):
            params: dict[str, str] = {
                "archived": "false",
                "limit": str(_PAGE_LIMIT),
                "properties": properties_param,
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
                raise ValueError("HubSpot companies API returned non-list `results`")
            all_companies.extend(results)

            next_cursor = _extract_next_cursor(payload)
            if next_cursor is None:
                break
            after = next_cursor

            if page_no + 1 >= _MAX_PAGES:
                logger.warning(
                    "HubSpot companies fetch hit _MAX_PAGES ceiling; stopping",
                    extra={
                        "integration_id": str(self.token_info.integration_id),
                        "page_limit": _PAGE_LIMIT,
                        "max_pages": _MAX_PAGES,
                        "collected": len(all_companies),
                    },
                )

        logger.info(
            "Fetched HubSpot companies",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "company_count": len(all_companies),
            },
        )
        return all_companies


def _make_request(params: dict[str, str]):
    """Factory binding this page's params; avoids closure-over-loop (ruff B023)."""

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
