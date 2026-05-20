"""HubSpot CRM Search fetcher for changed object ids.

This is the incremental-repair discovery layer only. It does not persist rows;
callers use the returned ids to fetch full objects through the normal refresh
paths before writing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)

logger = get_logger(__name__)

_BASE_URL = "https://api.hubapi.com/crm/v3/objects/{object_type}/search"
_DEFAULT_TIMEOUT = 30.0
_PAGE_LIMIT = 100
# This is an operational guardrail, not a business limit. If a tenant ever hits
# it, the run stops noisily instead of looping forever on a malformed cursor.
_MAX_PAGES = 5000

# HubSpot does not expose one uniform modified-time property across object
# types. Contacts use `lastmodifieddate`, while the other core objects use
# `hs_lastmodifieddate`; the tuple also lists fallback properties to read back.
_MODIFIED_PROPERTIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "deals": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
    "companies": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
    "contacts": ("lastmodifieddate", ("lastmodifieddate", "hs_lastmodifieddate")),
    "tasks": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
    "notes": ("hs_lastmodifieddate", ("hs_lastmodifieddate",)),
}


@dataclass(slots=True, frozen=True)
class ChangedHubSpotObject:
    """One changed HubSpot object discovered via CRM Search."""

    object_id: str
    source_modified_at: datetime | None
    raw: dict[str, Any]


class HubSpotChangedObjectsSearchFetcher:
    """Search HubSpot CRM for objects changed since a provider timestamp.

    This fetcher deliberately returns ids plus provider modified timestamps
    only. Callers must fetch the full object before writing so the incremental
    path cannot diverge from webhook and full-sync normalization rules.
    """

    def __init__(
        self,
        *,
        token_info: IntegrationTokenInfo,
        object_type: str,
        modified_since: datetime,
    ) -> None:
        if object_type not in _MODIFIED_PROPERTIES:
            raise ValueError(f"Unsupported HubSpot search object type: {object_type}")
        self.token_info = token_info
        self.object_type = object_type
        self.modified_since = _as_utc(modified_since)
        self._modified_property, self._properties = _MODIFIED_PROPERTIES[object_type]

    async def fetch_all(self) -> list[ChangedHubSpotObject]:
        """Return changed object ids sorted by provider modified timestamp.

        HubSpot Search pagination uses `paging.next.after`, which is only valid
        for this query shape. The durable cursor we store is still the provider
        modified timestamp, so replaying after a crash does not depend on a
        transient page token.
        """
        changed: list[ChangedHubSpotObject] = []
        after: str | None = None

        for page_no in range(_MAX_PAGES):
            body = self._build_body(after=after)
            # Token retry wraps only the provider request; no DB session is held
            # while HubSpot is searched or while OAuth refresh may occur.
            response = await call_with_token_retry(
                self.token_info,
                _make_request(self.object_type, body),
            )
            payload = response.json()
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("HubSpot search API returned non-list `results`")

            for raw in results:
                if not isinstance(raw, dict):
                    continue
                object_id = raw.get("id")
                if not isinstance(object_id, str) or not object_id:
                    continue
                changed.append(
                    ChangedHubSpotObject(
                        object_id=object_id,
                        source_modified_at=self._extract_modified_at(raw),
                        raw=raw,
                    )
                )

            next_cursor = _extract_next_cursor(payload)
            if next_cursor is None:
                break
            after = next_cursor

            if page_no + 1 >= _MAX_PAGES:
                logger.warning(
                    "HubSpot changed-object search hit _MAX_PAGES ceiling; stopping",
                    extra={
                        "integration_id": str(self.token_info.integration_id),
                        "object_type": self.object_type,
                        "page_limit": _PAGE_LIMIT,
                        "max_pages": _MAX_PAGES,
                        "collected": len(changed),
                    },
                )

        logger.info(
            "Fetched changed HubSpot object ids",
            extra={
                "integration_id": str(self.token_info.integration_id),
                "object_type": self.object_type,
                "changed_count": len(changed),
                "modified_since": self.modified_since.isoformat(),
            },
        )
        return changed

    def _build_body(self, *, after: str | None) -> dict[str, Any]:
        """Build the stable Search request used for timestamp-based replay."""
        body: dict[str, Any] = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": self._modified_property,
                            "operator": "GTE",
                            "value": self.modified_since.isoformat(),
                        }
                    ]
                }
            ],
            "sorts": [
                {
                    "propertyName": self._modified_property,
                    "direction": "ASCENDING",
                }
            ],
            "properties": list(self._properties),
            "limit": _PAGE_LIMIT,
        }
        if after is not None:
            body["after"] = after
        return body

    def _extract_modified_at(self, raw: dict[str, Any]) -> datetime | None:
        """Read the best provider-modified timestamp available on a result row."""
        properties = raw.get("properties")
        property_values = properties if isinstance(properties, dict) else {}
        candidates = [property_values.get(name) for name in self._properties]
        candidates.append(raw.get("updatedAt"))
        for candidate in candidates:
            parsed = _parse_datetime(candidate)
            if parsed is not None:
                return parsed
        return None


async def fetch_changed_hubspot_objects(
    *,
    token_info: IntegrationTokenInfo,
    object_type: str,
    modified_since: datetime,
) -> list[ChangedHubSpotObject]:
    """Discover changed HubSpot object ids for incremental sync."""
    return await HubSpotChangedObjectsSearchFetcher(
        token_info=token_info,
        object_type=object_type,
        modified_since=modified_since,
    ).fetch_all()


def _make_request(object_type: str, body: dict[str, Any]):
    """Build the retryable authenticated HubSpot Search request."""

    async def _request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.post(
                _BASE_URL.format(object_type=object_type),
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
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


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        return _as_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "ChangedHubSpotObject",
    "HubSpotChangedObjectsSearchFetcher",
    "fetch_changed_hubspot_objects",
]
