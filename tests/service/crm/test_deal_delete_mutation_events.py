from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_delete_deal_uses_delete_semantic_only() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        is_deleted=False,
        deal_status=CRMDealStatus.OPEN,
        properties={},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
        name="Delete Me",
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._record_mutation_event = AsyncMock()

    await service.delete_deal(deal.deal_id)

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.deleted"
    assert set(kwargs["changed_fields"]) == {"is_deleted", "deal_status"}
