"""HTTP response contracts for owner-scoped agent run history."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.automation.runs_handler import get_agent_run, list_agent_runs
from src.api.schemas.agent_run_models import AgentRunDetail, AgentRunListResponse
from src.domain.models.organisation_tables import (
    AgentRunStatus,
    AutomationAudienceMode,
    AutomationPrincipalType,
)


class Session:
    """Represent the Session test double used by the surrounding regression cases."""

    async def close(self) -> None:
        """Expose the close operation expected by the Session test double."""
        return None


class Service:
    """Represent the Service test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.session = Session()
        event_id = uuid4()
        principal_id = uuid4()
        self.detail = AgentRunDetail(
            run_id=uuid4(),
            agent_id=uuid4(),
            trigger_id=uuid4(),
            agent_revision=1,
            status=AgentRunStatus.SUCCEEDED,
            event_id=event_id,
            event_key="crm.deal.updated",
            correlation_id=str(uuid4()),
            created_by_user_id=principal_id,
            execution_principal_type=AutomationPrincipalType.USER,
            execution_principal_id=principal_id,
            audience_mode=AutomationAudienceMode.PRINCIPAL,
            audience_config={},
            subject_type="crm.deal",
            subject_id=str(uuid4()),
            source_domain="crm",
            source_system="copilotgtm",
            source_operation="update",
            source_event_id=str(event_id),
            actor_type="user",
            actor_id=str(principal_id),
            scheduled_for=None,
            caused_by_run_id=None,
            causation_depth=0,
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
            output_preview="Call buyer",
            created_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            started_at=None,
            completed_at=None,
            trigger_snapshot={},
            agent_snapshot={},
            input_snapshot={},
        )

    async def list_runs(self, **_kwargs):
        """Expose the list runs operation expected by the Service test double."""
        return AgentRunListResponse(items=[], next_cursor=None)

    async def get_run(self, _run_id):
        """Expose the get run operation expected by the Service test double."""
        return self.detail


@pytest.mark.asyncio
async def test_list_runs_is_read_only() -> None:
    """Verify the contract that list runs is read only."""
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
    """Verify the contract that get run returns full metadata."""
    service = Service()
    result = await get_agent_run(
        run_id=service.detail.run_id,
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert result.data.run_id == service.detail.run_id
    assert result.data.token_usage == {"total_tokens": 12}
