"""Regression tests for deal stage service boundary behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStageHistory
from src.service.crm.deals.stage_service import DealStageService


class _FakeStageSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_count = 0

    def add(self, obj: object) -> None:
        """Capture pending rows without requiring a database session."""
        self.added.append(obj)

    async def flush(self) -> None:
        """Count flush attempts so no-op stage changes can prove no DB churn."""
        self.flush_count += 1


@pytest.mark.asyncio
async def test_apply_stage_noops_when_stage_is_already_current() -> None:
    """Same-stage requests must not create audit rows or churn sync timestamps."""
    stage_id = uuid4()
    entered_at = datetime(2026, 6, 20, 10, 30, tzinfo=UTC)
    session = _FakeStageSession()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        stage_id=stage_id,
        stage_name="Discovery",
        stage_entered_at=entered_at,
        stage_updated_at=entered_at,
        source_modified_at=entered_at,
        synced_at=entered_at,
        last_seen_at=entered_at,
    )
    stage = SimpleNamespace(stage_id=stage_id, name="Discovery")

    result = await DealStageService(
        session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
    ).apply_stage(deal=deal, stage=stage)  # type: ignore[arg-type]

    assert result.changed is False
    assert result.stage_history is None
    assert result.changed_fields == {}
    assert deal.stage_entered_at == entered_at
    assert deal.stage_updated_at == entered_at
    assert deal.source_modified_at == entered_at
    assert session.flush_count == 0
    assert not any(isinstance(obj, CRMDealStageHistory) for obj in session.added)


@pytest.mark.asyncio
async def test_apply_stage_returns_created_stage_history_row() -> None:
    """Callers need the exact source row to project one stage-change activity."""
    organisation_id = uuid4()
    previous_stage_id = uuid4()
    next_stage_id = uuid4()
    session = _FakeStageSession()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        integration_id=uuid4(),
        stage_id=previous_stage_id,
        stage_name="Discovery",
        stage_entered_at=None,
        stage_updated_at=None,
        source_modified_at=None,
        synced_at=None,
        last_seen_at=None,
    )
    stage = SimpleNamespace(stage_id=next_stage_id, name="Proposal")

    result = await DealStageService(
        session=session,  # type: ignore[arg-type]
        organisation_id=organisation_id,
    ).apply_stage(deal=deal, stage=stage, source="api")  # type: ignore[arg-type]

    assert result.changed is True
    assert isinstance(result.stage_history, CRMDealStageHistory)
    assert result.stage_history in session.added
    assert result.stage_history.organisation_id == organisation_id
    assert result.stage_history.deal_id == deal.deal_id
    assert result.stage_history.stage_id == next_stage_id
    assert result.stage_history.from_stage_id == previous_stage_id
