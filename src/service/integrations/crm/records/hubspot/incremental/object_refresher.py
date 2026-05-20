"""Contracts for HubSpot incremental object refreshers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    ChangedHubSpotObject,
)


@dataclass(slots=True, frozen=True)
class HubSpotIncrementalRefreshResult:
    """Write outcome for one changed HubSpot object snapshot."""

    written_count: int = 0
    skipped_stale_count: int = 0


class HubSpotIncrementalObjectRefresher(Protocol):
    """Fetch and persist one changed HubSpot object discovered by Search."""

    async def refresh(
        self,
        *,
        token_info: IntegrationTokenInfo,
        changed_object: ChangedHubSpotObject,
    ) -> HubSpotIncrementalRefreshResult:
        """Refresh one provider object and return writer counters."""
        ...


def require_token_org_context(token_info: IntegrationTokenInfo) -> tuple[UUID, UUID]:
    """Return the org-scoped write context required by CRM record writers."""

    if token_info.organisation_id is None or token_info.integration_id is None:
        raise ValueError("HubSpot incremental refresh requires organisation_id and integration_id")
    return token_info.organisation_id, token_info.integration_id
