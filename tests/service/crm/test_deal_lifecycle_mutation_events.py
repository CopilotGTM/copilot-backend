from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.deals.write_service import DealWriteService


@pytest.mark.asyncio
async def test_close_deal_records_closed_and_stage_semantics_when_stage_changes() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=old_stage_id,
        stage_name="Proposal",
        deal_status=CRMDealStatus.OPEN,
        close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        properties={},
    )
    stage = SimpleNamespace(stage_id=new_stage_id, name="Closed Won", stage_state="won")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)
    service._stages.apply_stage = AsyncMock(
        side_effect=lambda deal, stage, source: setattr(deal, "stage_id", stage.stage_id)
        or setattr(deal, "stage_name", stage.name)
    )
    service._record_mutation_event = AsyncMock()

    await service.close_deal(
        deal.deal_id,
        outcome="won",
        closed_reason=None,
        close_date=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.closed"
    assert "stage_id" in kwargs["changed_fields"]
    assert "deal_status" in kwargs["changed_fields"]


@pytest.mark.asyncio
async def test_reopen_deal_omits_stage_changed_field_when_stage_is_same() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    stage_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=stage_id,
        stage_name="Discovery",
        deal_status=CRMDealStatus.WON,
        closed_won_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        closed_lost_at=None,
        closed_reason=None,
        properties={},
    )
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery", stage_state="open")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)
    service._stages.apply_stage = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.reopen_deal(deal.deal_id)

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.reopened"
    assert "stage_id" not in kwargs["changed_fields"]
    assert "deal_status" in kwargs["changed_fields"]


@pytest.mark.asyncio
async def test_reopen_open_deal_records_no_changed_fields_when_nothing_changes() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    stage_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=stage_id,
        stage_name="Discovery",
        deal_status=CRMDealStatus.OPEN,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        properties={},
    )
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery", stage_state="open")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)
    service._stages.apply_stage = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.reopen_deal(deal.deal_id)

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.reopened"
    assert kwargs["changed_fields"] == {}
