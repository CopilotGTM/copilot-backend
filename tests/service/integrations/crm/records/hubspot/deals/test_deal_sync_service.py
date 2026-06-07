from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.integrations.crm.records.hubspot.deals.deal_sync_service import (
    HubSpotDealRecordSyncService,
)


@pytest.mark.asyncio
async def test_build_property_list_includes_status_flags_when_property_discovery_is_empty() -> None:
    service = HubSpotDealRecordSyncService()

    async def _empty_discovery(*, token_info: object) -> set[str]:
        return set()

    service._fetch_property_names_with_retry = _empty_discovery  # type: ignore[method-assign]

    property_list = await service._build_property_list(
        token_info=SimpleNamespace(integration_id=uuid4())
    )

    assert "hs_is_closed" in property_list
    assert "hs_is_closed_won" in property_list
    assert "hs_is_closed_lost" in property_list
