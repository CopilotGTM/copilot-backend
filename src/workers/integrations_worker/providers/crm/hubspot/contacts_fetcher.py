"""HubSpot contacts fetcher.

GET /crm/v3/objects/contacts?archived=false paginated via `paging.next.after`.

No classification, no normalisation, no DB. Pure HTTP + pagination + token
refresh. Raises on malformed responses; retries / auth recovery are handled
by `call_with_token_retry` and the caller's Celery retry policy.

Property list is a curated static set. `associatedcompanyid` carries the
primary-company HubSpot id — the writer resolves that to a FK against the
live crm_companies snapshot.
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

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
_DEFAULT_TIMEOUT = 30.0
_PAGE_LIMIT = 100
_MAX_PAGES = 5000

_CONTACT_PROPERTIES: tuple[str, ...] = (
    "firstname",
    "lastname",
    "jobtitle",
    "email",
    "hs_additional_emails",
    "phone",
    "mobilephone",
    "fax",
    "website",
    "company",
    "associatedcompanyid",
    "hubspot_owner_id",
    "lifecyclestage",
    "hs_lead_status",
    "hs_marketable_status",
    "address",
    "city",
    "state",
    "zip",
    "country",
    "hs_timezone",
    "notes_last_contacted",
    "notes_last_updated",
    "createdate",
    "lastmodifieddate",
    "hs_createdate",
    "hs_lastmodifieddate",
)


class HubSpotContactsFetcher:
    """Fetch all (non-archived) HubSpot contacts for the integration."""

    def __init__(self, *, token_info: IntegrationTokenInfo) -> None:
        self.token_info = token_info

    async def fetch_all(self) -> list[dict[str, Any]]:
        """Return every contact as a raw dict. Archived records filtered server-side."""
        all_contacts: list[dict[str, Any]] = []
        after: str | None = None
        properties_param = ",".join(_CONTACT_PROPERTIES)

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
                raise ValueError("HubSpot contacts API returned non-list `results`")
            all_contacts.extend(results)

            next_cursor = _extract_next_cursor(payload)
            if next_cursor is None:
                break
            after = next_cursor

            if page_no + 1 >= _MAX_PAGES:
                logger.warning(
                    "HubSpot contacts fetch hit _MAX_PAGES ceiling; stopping",
                    extra={
                        "integration_id": str(self.token_info.integration_id),
                        "page_limit": _PAGE_LIMIT,
                        "max_pages": _MAX_PAGES,
                        "collected": len(all_contacts),
                    },
                )

        logger.info(
            "Fetched HubSpot contacts",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "contact_count": len(all_contacts),
            },
        )
        return all_contacts


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
