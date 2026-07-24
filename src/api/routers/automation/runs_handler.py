"""Read-only endpoints for one user's durable agent execution history."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.agent_run_models import AgentRunDetail, AgentRunListResponse
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables import AgentRunStatus
from src.infra.utils.logger import get_logger
from src.service.automation.runs.read_service import AgentRunReadError, AgentRunReadService

logger = get_logger("agent_runs_handler")
agent_runs_router = APIRouter()


async def get_agent_run_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session=Depends(get_org_async_session),
) -> AsyncIterator[AgentRunReadService]:
    """Bind run reads to authenticated tenant and owner identity."""

    service = AgentRunReadService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
    )
    try:
        yield service
    finally:
        await service.close()


@agent_runs_router.get(
    "/agent-runs",
    response_model=GenericResponse[AgentRunListResponse],
    summary="List agent runs",
)
async def list_agent_runs(
    response: FastAPIResponse,
    agent_id: UUID | None = Query(default=None),
    trigger_id: UUID | None = Query(default=None),
    status: AgentRunStatus | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    service: AgentRunReadService = Depends(get_agent_run_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentRunListResponse]:
    """Return a compact keyset page of owned runs."""

    try:
        page = await service.list_runs(
            agent_id=agent_id,
            trigger_id=trigger_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )
        return Response.success_response("Agent runs retrieved successfully", data=page)
    except AgentRunReadError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error listing agent runs")
        return Response.internal_server_error_response(
            "An unexpected error occurred while processing your request",
            response=response,
        )


@agent_runs_router.get(
    "/agent-runs/{run_id}",
    response_model=GenericResponse[AgentRunDetail],
    summary="Get agent run",
)
async def get_agent_run(
    run_id: UUID,
    response: FastAPIResponse,
    service: AgentRunReadService = Depends(get_agent_run_read_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentRunDetail]:
    """Return snapshots, output, usage, and timing for one owned run."""

    try:
        item = await service.get_run(run_id)
        return Response.success_response("Agent run retrieved successfully", data=item)
    except AgentRunReadError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error retrieving agent run")
        return Response.internal_server_error_response(
            "An unexpected error occurred while processing your request",
            response=response,
        )
