"""HTTP response contracts for owner-scoped agent run history."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.automation.runs_handler import get_agent_run, list_agent_runs
from src.api.schemas.agent_run_models import AgentRunDetail, AgentRunListResponse
from src.domain.models.organisation_tables import AgentRunStatus


class Session:
    async def close(self) -> None:
        return None


class Service:
    def __init__(self) -> None:
        self.session = Session()
        self.detail = AgentRunDetail(
            run_id=uuid4(),
            agent_id=uuid4(),
            trigger_id=uuid4(),
            agent_revision=1,
            status=AgentRunStatus.SUCCEEDED,
            source_domain="crm",
            event_type="crm.deal.stage_changed",
            source_event_id=str(uuid4()),
            scheduled_for=None,
            attempt_count=1,
            claimed_at=None,
            claimed_by=None,
            model_name="gpt-5-mini",
            token_usage={"total_tokens": 12},
            cost=None,
            latency_ms=100,
            error_code=None,
            error_message=None,
            output_text="Call buyer",
            output_json=None,
            created_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            started_at=None,
            completed_at=None,
            trigger_snapshot={},
            agent_snapshot={},
            input_snapshot={},
        )

    async def list_runs(self, **_kwargs):
        return AgentRunListResponse(items=[], next_cursor=None)

    async def get_run(self, _run_id):
        return self.detail


@pytest.mark.asyncio
async def test_list_runs_is_read_only() -> None:
    result = await list_agent_runs(
        response=FastAPIResponse(),
        agent_id=None,
        trigger_id=None,
        status=None,
        cursor=None,
        limit=20,
        service=Service(),  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert result.data.next_cursor is None


@pytest.mark.asyncio
async def test_get_run_returns_full_metadata() -> None:
    service = Service()
    result = await get_agent_run(
        run_id=service.detail.run_id,
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert result.data.run_id == service.detail.run_id
    assert result.data.token_usage == {"total_tokens": 12}
