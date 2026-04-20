from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStageHistory
from src.service.crm.internal.deal_write_service import DealWriteService


class _FakeOrgSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_move_stage_records_previous_stage_context_and_updates_stage_timestamps() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    previous_stage_id = uuid4()
    next_stage_id = uuid4()
    previous_stage_entered_at = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    previous_stage_updated_at = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        deal_id=uuid4(),
        pipeline_id=uuid4(),
        integration_id=uuid4(),
        stage_id=previous_stage_id,
        stage_external_id="stage-old",
        stage_name="Discovery",
        stage_entered_at=previous_stage_entered_at,
        stage_updated_at=previous_stage_updated_at,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    stage = SimpleNamespace(stage_id=next_stage_id, external_id="stage-new", name="Proposal")

    service._load_internal_deal = AsyncMock(return_value=deal)
    service._load_stage = AsyncMock(return_value=stage)

    updated = await service.move_stage(deal.deal_id, next_stage_id)

    assert updated is deal
    assert deal.stage_id == next_stage_id
    assert deal.stage_external_id == "stage-new"
    assert deal.stage_name == "Proposal"
    assert deal.stage_entered_at is not None
    assert deal.stage_updated_at is not None
    assert deal.stage_entered_at == deal.stage_updated_at
    assert deal.stage_entered_at > previous_stage_entered_at

    history = next(obj for obj in session.added if isinstance(obj, CRMDealStageHistory))
    assert history.stage_id == next_stage_id
    assert history.stage_external_id == "stage-new"
    assert history.from_stage_id == previous_stage_id
    assert history.from_stage_external_id == "stage-old"
    assert history.source == "api"


@pytest.mark.asyncio
async def test_create_deal_sets_initial_stage_timestamps_and_history_source() -> None:
    session = _FakeOrgSession()
    service = DealWriteService(
        org_session=session,
        master_session=SimpleNamespace(),
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
    )

    pipeline_id = uuid4()
    stage_id = uuid4()
    integration_id = uuid4()
    pipeline = SimpleNamespace(pipeline_id=pipeline_id, external_id="pipe-1", name="Sales")
    stage = SimpleNamespace(stage_id=stage_id, external_id="stage-1", name="Discovery")
    integration = SimpleNamespace(integration_id=integration_id)

    service._get_default_pipeline = AsyncMock(return_value=pipeline)
    service._get_first_stage = AsyncMock(return_value=stage)

    with patch(
        "src.service.crm.internal.deal_write_service.InternalCRMSetupService.get_internal_integration",
        new=AsyncMock(return_value=integration),
    ):
        deal = await service.create_deal({"name": "New Deal"})

    assert deal.stage_id == stage_id
    assert deal.stage_external_id == "stage-1"
    assert deal.stage_entered_at is not None
    assert deal.stage_updated_at is not None
    assert deal.stage_entered_at == deal.stage_updated_at

    history = next(obj for obj in session.added if isinstance(obj, CRMDealStageHistory))
    assert history.stage_id == stage_id
    assert history.stage_external_id == "stage-1"
    assert history.from_stage_id is None
    assert history.from_stage_external_id is None
    assert history.source == "api"
