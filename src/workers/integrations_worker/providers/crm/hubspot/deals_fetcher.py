"""HubSpot deals fetcher (phase 6).

GET /crm/v3/objects/deals?archived=false paginated via `paging.next.after`.

Unlike the companies/contacts fetchers, the property list is NOT hardcoded —
the caller passes it in. The dealt-with philosophy is "capture the complete
HubSpot deal payload": the sync service asks the HubSpot Properties API for
every active property on the portal (system + custom), builds the list once,
and hands it to this fetcher. Canonical properties become dedicated CRMDeal
columns; everything else overflows into the `properties` JSONB so nothing is
silently dropped.

We also request:
  - `associations=contacts,companies,line_items` so primary-company /
    primary-contact linkage comes back inline per record AND the deal
    association graph (incl. line items) is captured in one round-trip.
  - `propertiesWithHistory=<full property list>` so the stage history writer
    can extract every `dealstage` / `pipeline` / `amount` / `closedate` /
    custom-property transition from `data_payload.propertiesWithHistory`
    without a second HubSpot call. Defaults to "every property the caller
    asked for", which mirrors the user requirement of capturing the full
    deal audit trail.

Caveat: HubSpot caps page `limit` to 50 when `propertiesWithHistory` is
requested (vs 100 for property-only calls). We always pass a history list
when the caller has properties, so `_PAGE_LIMIT = 50` is deliberate.
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

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/deals"
_DEFAULT_TIMEOUT = 60.0
# HubSpot caps propertiesWithHistory requests to 50 records/page. We always
# request history on a handful of tracked properties for phase 7, so we hold
# at 50 even when the caller passes an empty history list (keeps the fetcher
# shape uniform + conservative).
_PAGE_LIMIT = 50
# Up to 500k deals per run (50 * 10_000 pages). WARN on ceiling.
_MAX_PAGES = 10_000

# When the caller doesn't pass `history_properties` explicitly we mirror the
# full property list — capturing the audit trail for everything HubSpot can
# tell us about. The sync service typically passes its own list (often the
# entire dynamic property catalogue) so this default rarely fires.
_DEFAULT_ASSOCIATIONS: tuple[str, ...] = ("contacts", "companies", "line_items")


class HubSpotDealsFetcher:
    """Fetch all (non-archived) HubSpot deals for the integration."""

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        properties: list[str],
        history_properties: tuple[str, ...] | None = None,
        associations: tuple[str, ...] = _DEFAULT_ASSOCIATIONS,
    ) -> None:
        if not properties:
            raise ValueError("HubSpotDealsFetcher requires a non-empty properties list")
        self.token_info = token_info
        # Dedup + sort for a stable query string — makes log output and
        # request fingerprints predictable across retries.
        self._properties = sorted(set(properties))
        # `None` means "track history on every requested property" — the
        # broadest possible audit trail. Callers pass an explicit tuple to
        # narrow the cost-vs-completeness trade-off.
        self._history_properties: tuple[str, ...] = (
            tuple(self._properties) if history_properties is None else history_properties
        )
        self._associations = associations

    async def fetch_all(self) -> list[dict[str, Any]]:
        """Return every deal as a raw dict. Archived records filtered server-side."""
        all_deals: list[dict[str, Any]] = []
        after: str | None = None
        properties_param = ",".join(self._properties)
        history_param = ",".join(self._history_properties)
        associations_param = ",".join(self._associations)

        for page_no in range(_MAX_PAGES):
            params: dict[str, str] = {
                "archived": "false",
                "limit": str(_PAGE_LIMIT),
                "properties": properties_param,
            }
            if history_param:
                params["propertiesWithHistory"] = history_param
            if associations_param:
                params["associations"] = associations_param
            if after is not None:
                params["after"] = after

            response = await call_with_token_retry(
                self.token_info,
                _make_request(params),
            )
            payload = response.json()
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("HubSpot deals API returned non-list `results`")
            all_deals.extend(results)

            next_cursor = _extract_next_cursor(payload)
            if next_cursor is None:
                break
            after = next_cursor

            if page_no + 1 >= _MAX_PAGES:
                logger.warning(
                    "HubSpot deals fetch hit _MAX_PAGES ceiling; stopping",
                    extra={
                        "integration_id": str(self.token_info.integration_id),
                        "page_limit": _PAGE_LIMIT,
                        "max_pages": _MAX_PAGES,
                        "collected": len(all_deals),
                    },
                )

        logger.info(
            "Fetched HubSpot deals",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "deal_count": len(all_deals),
                "property_list_size": len(self._properties),
            },
        )
        return all_deals


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
