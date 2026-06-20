from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_create_deal_does_not_emit_stage_or_owner_semantics() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.created",
        operation="create_deal",
        changed_fields={
            "stage_id": {"old": None, "new": str(uuid4())},
            "owner_id": {"old": None, "new": str(uuid4())},
        },
        summary="Created deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["semantic_event_types"] == ["crm.deal.created"]


@pytest.mark.asyncio
async def test_update_deal_description_is_audit_only() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(deal_id=uuid4(), integration_id=uuid4())
    service._mutation_events.record_event = AsyncMock()

    await service._record_mutation_event(
        deal=deal,
        event_type="crm.deal.updated",
        operation="update_deal",
        changed_fields={"description": {"old": "old", "new": "new"}},
        summary="Updated deal",
    )

    kwargs = service._mutation_events.record_event.await_args.kwargs
    assert kwargs["semantic_event_types"] == []


@pytest.mark.asyncio
async def test_update_deal_same_value_records_no_changed_fields() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(add=lambda obj: None, flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Existing",
        amount=1000,
        currency="USD",
        deal_status="open",
        close_date=None,
        expected_close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        crm_url=None,
        source_created_at=None,
        last_activity_at=None,
        description=None,
        deal_type=None,
        priority=None,
        lead_source=None,
        probability=None,
        forecast_category=None,
        properties={},
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._record_mutation_event = AsyncMock()

    await service.update_deal(deal.deal_id, {"amount": 1000})

    service._record_mutation_event.assert_awaited_once()
    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["changed_fields"] == {}
