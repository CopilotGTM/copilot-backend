"""Tenant-scoped agent-run history and notification-repair endpoints.

The Agents UI reads persisted execution output here. Its only mutation retries
an existing delivery target after a repair; it never reruns the agent.
"""

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
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.agent_run_models import (
    AgentRunDeliveryRetryRequest,
    AgentRunDeliveryRetryResponse,
    AgentRunDetail,
    AgentRunListResponse,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables import AgentRunStatus
from src.infra.utils.logger import get_logger
from src.service.automation.runs.read_service import AgentRunReadError, AgentRunReadService
from src.workers.chat_worker.client import send_chat_task_async

logger = get_logger("agent_runs_handler")
agent_runs_router = APIRouter()


async def get_agent_run_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session=Depends(get_org_async_session),
) -> AsyncIterator[AgentRunReadService]:
    """Bind run history operations to the authenticated tenant and user."""

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


@agent_runs_router.post(
    "/agent-runs/{run_id}/delivery/retry",
    response_model=GenericResponse[AgentRunDeliveryRetryResponse],
    status_code=202,
    summary="Retry repaired agent run delivery",
)
async def retry_agent_run_delivery(
    run_id: UUID,
    body: AgentRunDeliveryRetryRequest,
    response: FastAPIResponse,
    service: AgentRunReadService = Depends(get_agent_run_read_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AgentRunDeliveryRetryResponse]:
    """Retry an owner-visible pre-provider failure without rerunning the agent."""

    try:
        item, delivery_ids = await service.retry_delivery(run_id, body.delivery_id)
        # Persist the pending state before publishing. A broker failure then leaves
        # the row visible to the scheduled recovery sweep instead of hiding the retry.
        await service.session.commit()
    except AgentRunReadError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error retrying agent run delivery")
        return Response.internal_server_error_response(
            "An unexpected error occurred while processing your request",
            response=response,
        )

    try:
        for delivery_id in delivery_ids:
            await send_chat_task_async(
                "chat.deliver_agent_run",
                kwargs={
                    "organisation_id": str(service.organisation_id),
                    "delivery_id": str(delivery_id),
                },
                queue="chat_messages",
                routing_key="chat_messages",
            )
    except Exception:
        # The committed pending row remains visible to the scheduled recovery sweep.
        logger.exception(
            "Agent delivery retry publication failed org=%s run=%s",
            service.organisation_id,
            run_id,
        )
    return Response.accepted_response(
        "Agent run delivery retry accepted",
        data=item,
        response=response,
    )
