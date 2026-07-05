from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.api.routers.crm.pipelines_handler import _build_pipeline_item, _build_stage_item
from src.service.crm.access.pipeline_access_service import PipelineAccessService


class _NoRowsResult:
    def scalars(self) -> _NoRowsResult:
        return self

    def all(self) -> list[object]:
        return []


class _CaptureSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, stmt: object) -> _NoRowsResult:
        self.statements.append(stmt)
        return _NoRowsResult()


@pytest.mark.asyncio
async def test_list_pipelines_can_filter_to_active_only() -> None:
    session = _CaptureSession()
    service = PipelineAccessService(session=session, organisation_id=uuid4())  # type: ignore[arg-type]

    await service.list_pipelines(integration_id=uuid4(), active_only=True)

    assert "crm_pipelines.is_active IS true" in str(session.statements[0])


def test_pipeline_response_includes_deal_counts() -> None:
    stage = SimpleNamespace(
        stage_id=uuid4(),
        name="Qualified",
        label=None,
        stage_state="open",
        probability=25,
        is_closed=False,
        display_order=1,
    )
    pipeline = SimpleNamespace(
        pipeline_id=uuid4(),
        name="Sales",
        label=None,
        is_default=True,
        is_active=True,
        display_order=1,
        updated_at=datetime.now(UTC),
    )

    assert _build_stage_item(stage).deal_count is None
    assert _build_stage_item(stage, deal_count=0).deal_count == 0
    assert _build_stage_item(stage, deal_count=2).deal_count == 2
    assert _build_pipeline_item(pipeline, [stage]).deal_count is None
    assert _build_pipeline_item(pipeline, [stage], deal_count=0).deal_count == 0
    assert _build_pipeline_item(pipeline, [stage], deal_count=3).deal_count == 3


@pytest.mark.asyncio
async def test_pipeline_access_service_counts_deals_by_pipeline_and_stage() -> None:
    session = _CaptureSession()
    session.execute = _execute_rows(  # type: ignore[method-assign]
        session,
        [(uuid4(), 1), (uuid4(), 3)],
        [(uuid4(), 2)],
    )
    service = PipelineAccessService(session=session, organisation_id=uuid4())  # type: ignore[arg-type]

    pipeline_counts = await service.count_deals_by_pipeline([uuid4(), uuid4()])
    stage_counts = await service.count_deals_by_stage([uuid4()])

    assert sorted(pipeline_counts.values()) == [1, 3]
    assert sorted(stage_counts.values()) == [2]
    assert "crm_deals.pipeline_id" in str(session.statements[0])
    assert "crm_deals.stage_id" in str(session.statements[1])


def _execute_rows(session: _CaptureSession, *row_sets: list[tuple[object, int]]):
    rows = list(row_sets)

    async def execute(stmt: object):
        class _Rows:
            def all(self) -> list[tuple[object, int]]:
                return rows.pop(0)

        session.statements.append(stmt)
        return _Rows()

    return execute
