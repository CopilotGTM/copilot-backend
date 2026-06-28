from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.deals.contracts import DealStageChangeResult, DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService


def _stage_result(deal, changed_fields: dict | None = None) -> DealStageChangeResult:
    return DealStageChangeResult(
        deal=deal,
        changed_fields=changed_fields or {},
        changed=bool(changed_fields),
        stage_history=None,
    )


@pytest.mark.asyncio
async def test_move_stage_rejects_invalid_stage_id_before_loading_deal() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.move_stage(uuid4(), "not-a-uuid")  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid stage_id"
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_deal_rejects_invalid_raw_inputs_before_loading_deal() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.close_deal(
            uuid4(),
            outcome=123,  # type: ignore[arg-type]
            closed_reason=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "outcome must be 'won' or 'lost'"
    service._load_internal_deal.assert_not_awaited()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.close_deal(
            uuid4(),
            outcome="won",
            closed_reason=None,
            close_date="2026-07-31",  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "close_date must be a datetime"
    service._load_internal_deal.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopen_deal_rejects_invalid_stage_id_before_loading_deal() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    service._load_internal_deal = AsyncMock()

    with pytest.raises(DealWriteServiceError) as exc_info:
        await service.reopen_deal(uuid4(), stage_id="not-a-uuid")  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Invalid stage_id"
    service._load_internal_deal.assert_not_awaited()


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

    async def apply_stage(*, deal, stage, source):
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        return _stage_result(
            deal,
            {
                "stage_id": {"old": old_stage_id, "new": new_stage_id},
                "stage_name": {"old": "Proposal", "new": "Closed Won"},
            },
        )

    service._stages.apply_stage = AsyncMock(side_effect=apply_stage)
    service._projection.upsert_stage_change = AsyncMock()
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
async def test_close_deal_as_won_clears_closed_reason() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    lost_at = datetime(2026, 6, 19, 10, 0, tzinfo=UTC)
    won_at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=old_stage_id,
        stage_name="Closed Lost",
        deal_status=CRMDealStatus.LOST,
        close_date=lost_at,
        closed_won_at=None,
        closed_lost_at=lost_at,
        closed_reason="No budget",
        properties={},
    )
    stage = SimpleNamespace(stage_id=new_stage_id, name="Closed Won", stage_state="won")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)

    async def apply_stage(*, deal, stage, source):
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        return _stage_result(
            deal,
            {
                "stage_id": {"old": old_stage_id, "new": new_stage_id},
                "stage_name": {"old": "Closed Lost", "new": "Closed Won"},
            },
        )

    service._stages.apply_stage = AsyncMock(side_effect=apply_stage)
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.close_deal(
        deal.deal_id,
        outcome="won",
        closed_reason="Contract signed",
        close_date=won_at,
    )

    assert deal.deal_status == CRMDealStatus.WON
    assert deal.close_date == won_at
    assert deal.closed_won_at == won_at
    assert deal.closed_lost_at is None
    assert deal.closed_reason is None
    changed_fields = service._record_mutation_event.await_args.kwargs["changed_fields"]
    assert changed_fields["closed_reason"] == {"old": "No budget", "new": None}


@pytest.mark.asyncio
async def test_close_deal_as_lost_trims_closed_reason() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    lost_at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
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
    stage = SimpleNamespace(stage_id=new_stage_id, name="Closed Lost", stage_state="lost")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)

    async def apply_stage(*, deal, stage, source):
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        return _stage_result(deal)

    service._stages.apply_stage = AsyncMock(side_effect=apply_stage)
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.close_deal(
        deal.deal_id,
        outcome="lost",
        closed_reason="  Budget frozen  ",
        close_date=lost_at,
    )

    assert deal.closed_reason == "Budget frozen"
    changed_fields = service._record_mutation_event.await_args.kwargs["changed_fields"]
    assert changed_fields["closed_reason"] == {"old": None, "new": "Budget frozen"}


@pytest.mark.asyncio
async def test_move_stage_to_lost_stage_closes_deal_lifecycle() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    moved_at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
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
    stage = SimpleNamespace(stage_id=new_stage_id, name="Closed Lost", stage_state="lost")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._load_stage = AsyncMock(return_value=stage)

    async def apply_stage(*, deal, stage, source):
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        deal.stage_entered_at = moved_at
        return _stage_result(
            deal,
            {
                "stage_id": {"old": old_stage_id, "new": new_stage_id},
                "stage_name": {"old": "Proposal", "new": "Closed Lost"},
            },
        )

    service._stages.apply_stage = AsyncMock(side_effect=apply_stage)
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.move_stage(deal.deal_id, stage.stage_id)

    assert deal.deal_status == CRMDealStatus.LOST
    assert deal.close_date == moved_at
    assert deal.closed_lost_at == moved_at
    assert deal.closed_won_at is None
    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.closed"
    assert kwargs["operation"] == "move_stage"
    assert "stage_id" in kwargs["changed_fields"]
    assert "deal_status" in kwargs["changed_fields"]
    assert "closed_lost_at" in kwargs["changed_fields"]


@pytest.mark.asyncio
async def test_move_stage_to_won_stage_closes_deal_lifecycle() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    moved_at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
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
    service._load_stage = AsyncMock(return_value=stage)

    async def apply_stage(*, deal, stage, source):
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        deal.stage_entered_at = moved_at
        return _stage_result(
            deal,
            {
                "stage_id": {"old": old_stage_id, "new": new_stage_id},
                "stage_name": {"old": "Proposal", "new": "Closed Won"},
            },
        )

    service._stages.apply_stage = AsyncMock(side_effect=apply_stage)
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.move_stage(deal.deal_id, stage.stage_id)

    assert deal.deal_status == CRMDealStatus.WON
    assert deal.close_date == moved_at
    assert deal.closed_won_at == moved_at
    assert deal.closed_lost_at is None
    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.closed"
    assert kwargs["operation"] == "move_stage"
    assert "stage_id" in kwargs["changed_fields"]
    assert "deal_status" in kwargs["changed_fields"]
    assert "closed_won_at" in kwargs["changed_fields"]


@pytest.mark.asyncio
async def test_move_stage_from_lost_to_won_clears_lost_reason() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    lost_at = datetime(2026, 6, 19, 10, 0, tzinfo=UTC)
    moved_at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=old_stage_id,
        stage_name="Closed Lost",
        deal_status=CRMDealStatus.LOST,
        close_date=lost_at,
        closed_won_at=None,
        closed_lost_at=lost_at,
        closed_reason="No budget",
        properties={},
    )
    stage = SimpleNamespace(stage_id=new_stage_id, name="Closed Won", stage_state="won")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._load_stage = AsyncMock(return_value=stage)

    async def apply_stage(*, deal, stage, source):
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        deal.stage_entered_at = moved_at
        return _stage_result(
            deal,
            {
                "stage_id": {"old": old_stage_id, "new": new_stage_id},
                "stage_name": {"old": "Closed Lost", "new": "Closed Won"},
            },
        )

    service._stages.apply_stage = AsyncMock(side_effect=apply_stage)
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.move_stage(deal.deal_id, stage.stage_id)

    assert deal.deal_status == CRMDealStatus.WON
    assert deal.close_date == moved_at
    assert deal.closed_won_at == moved_at
    assert deal.closed_lost_at is None
    assert deal.closed_reason is None
    changed_fields = service._record_mutation_event.await_args.kwargs["changed_fields"]
    assert changed_fields["closed_reason"] == {"old": "No budget", "new": None}


@pytest.mark.asyncio
async def test_move_stage_to_open_stage_reopens_closed_deal_lifecycle() -> None:
    service = DealWriteService(
        org_session=SimpleNamespace(flush=AsyncMock()),
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )
    old_stage_id = uuid4()
    new_stage_id = uuid4()
    closed_at = datetime(2026, 6, 19, 10, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=old_stage_id,
        stage_name="Closed Lost",
        deal_status=CRMDealStatus.LOST,
        close_date=closed_at,
        closed_won_at=None,
        closed_lost_at=closed_at,
        closed_reason="No budget",
        properties={},
    )
    stage = SimpleNamespace(stage_id=new_stage_id, name="Qualification", stage_state="open")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._load_stage = AsyncMock(return_value=stage)

    async def apply_stage(*, deal, stage, source):
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        return _stage_result(
            deal,
            {
                "stage_id": {"old": old_stage_id, "new": new_stage_id},
                "stage_name": {"old": "Closed Lost", "new": "Qualification"},
            },
        )

    service._stages.apply_stage = AsyncMock(side_effect=apply_stage)
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.move_stage(deal.deal_id, stage.stage_id)

    assert deal.deal_status == CRMDealStatus.OPEN
    assert deal.close_date is None
    assert deal.closed_lost_at is None
    assert deal.closed_won_at is None
    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.reopened"
    assert kwargs["operation"] == "move_stage"
    assert "stage_id" in kwargs["changed_fields"]
    assert "deal_status" in kwargs["changed_fields"]
    assert "close_date" in kwargs["changed_fields"]
    assert "closed_lost_at" in kwargs["changed_fields"]


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
        close_date=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        closed_won_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        closed_lost_at=None,
        closed_reason=None,
        properties={},
    )
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery", stage_state="open")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)
    service._stages.apply_stage = AsyncMock(return_value=_stage_result(deal))
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.reopen_deal(deal.deal_id)

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.reopened"
    assert "stage_id" not in kwargs["changed_fields"]
    assert "deal_status" in kwargs["changed_fields"]
    assert "close_date" in kwargs["changed_fields"]
    assert deal.close_date is None


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
        close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        closed_reason=None,
        properties={},
    )
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery", stage_state="open")
    service._load_internal_deal = AsyncMock(return_value=deal)
    service._resolve_stage_for_state = AsyncMock(return_value=stage)
    service._stages.apply_stage = AsyncMock(return_value=_stage_result(deal))
    service._projection.upsert_stage_change = AsyncMock()
    service._record_mutation_event = AsyncMock()

    await service.reopen_deal(deal.deal_id)

    kwargs = service._record_mutation_event.await_args.kwargs
    assert kwargs["event_type"] == "crm.deal.reopened"
    assert kwargs["changed_fields"] == {}
