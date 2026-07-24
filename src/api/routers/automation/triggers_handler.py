"""Versioned HTTP endpoints for user-owned agent trigger definitions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCatalog,
    AutomationTriggerCreateRequest,
    AutomationTriggerDetail,
    AutomationTriggerListResponse,
    AutomationTriggerRefreshRequest,
    AutomationTriggerUpdateRequest,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables import AutomationTriggerStatus
from src.infra.utils.logger import get_logger
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.service import AutomationTriggerService

logger = get_logger("automation_triggers_handler")
_UNEXPECTED_ERROR_MESSAGE = "An unexpected error occurred while processing your request"

automation_triggers_router = APIRouter()


async def get_automation_trigger_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
) -> AsyncIterator[AutomationTriggerService]:
    """Bind trigger access to authenticated organization and owner identity."""

    service = AutomationTriggerService(org_session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        await service.close()


@automation_triggers_router.get(
    "/automation-trigger-catalog",
    response_model=GenericResponse[AutomationTriggerCatalog],
    summary="Get automation trigger catalog",
)
async def get_automation_trigger_catalog(
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AutomationTriggerCatalog]:
    """Return supported trigger contracts and organization CRM options."""

    try:
        return Response.success_response(
            "Trigger catalog retrieved successfully", data=await service.get_catalog()
        )
    except Exception:
        logger.exception("Unexpected error retrieving trigger catalog")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.post(
    "/agents/{agent_id}/triggers",
    response_model=GenericResponse[AutomationTriggerDetail],
    status_code=http_status.HTTP_201_CREATED,
    summary="Create agent trigger",
)
async def create_agent_trigger(
    agent_id: UUID,
    body: AutomationTriggerCreateRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationTriggerDetail]:
    """Create and pin one validated trigger in a single transaction."""

    try:
        item = await service.create_trigger(agent_id, body)
        await service.session.commit()
        response.headers["Location"] = f"/users/v1/agents/{agent_id}/triggers/{item.trigger_id}"
        return Response.created_response(
            "Trigger created successfully", data=item, response=response
        )
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error creating automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.get(
    "/agents/{agent_id}/triggers",
    response_model=GenericResponse[AutomationTriggerListResponse],
    summary="List agent triggers",
)
async def list_agent_triggers(
    agent_id: UUID,
    response: FastAPIResponse,
    status: AutomationTriggerStatus | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AutomationTriggerListResponse]:
    """List non-archived triggers for one owned agent."""

    try:
        data = await service.list_triggers(agent_id, status=status, limit=limit, offset=offset)
        return Response.success_response("Triggers retrieved successfully", data=data)
    except AutomationServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error listing automation triggers")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.get(
    "/agents/{agent_id}/triggers/{trigger_id}",
    response_model=GenericResponse[AutomationTriggerDetail],
    summary="Get agent trigger",
)
async def get_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AutomationTriggerDetail]:
    """Return one owned trigger with its pinned target snapshot."""

    try:
        return Response.success_response(
            "Trigger retrieved successfully",
            data=await service.get_trigger(agent_id, trigger_id),
        )
    except AutomationServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.put(
    "/agents/{agent_id}/triggers/{trigger_id}",
    response_model=GenericResponse[AutomationTriggerDetail],
    summary="Replace agent trigger",
)
async def replace_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    body: AutomationTriggerUpdateRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationTriggerDetail]:
    """Replace configuration while preserving the explicit agent revision pin."""

    try:
        item = await service.replace_trigger(agent_id, trigger_id, body)
        await service.session.commit()
        return Response.success_response("Trigger updated successfully", data=item)
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error replacing automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.post(
    "/agents/{agent_id}/triggers/{trigger_id}/refresh-target",
    response_model=GenericResponse[AutomationTriggerDetail],
    summary="Refresh trigger target",
)
async def refresh_agent_trigger_target(
    agent_id: UUID,
    trigger_id: UUID,
    body: AutomationTriggerRefreshRequest,
    response: FastAPIResponse,
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AutomationTriggerDetail]:
    """Explicitly repin one trigger to the agent's current revision."""

    try:
        item = await service.refresh_target(
            agent_id, trigger_id, expected_updated_at=body.expected_updated_at
        )
        await service.session.commit()
        return Response.success_response("Trigger target refreshed successfully", data=item)
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error refreshing automation trigger target")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)


@automation_triggers_router.delete(
    "/agents/{agent_id}/triggers/{trigger_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Archive agent trigger",
)
async def archive_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    response: FastAPIResponse,
    expected_updated_at: datetime = Query(),
    service: AutomationTriggerService = Depends(get_automation_trigger_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> FastAPIResponse | GenericResponse[None]:
    """Soft-delete one trigger using optimistic concurrency."""

    try:
        if expected_updated_at.tzinfo is None or expected_updated_at.utcoffset() is None:
            raise AutomationServiceError("expected_updated_at must include timezone")
        await service.archive_trigger(agent_id, trigger_id, expected_updated_at=expected_updated_at)
        await service.session.commit()
        return Response.no_content_response()
    except AutomationServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code, message=exc.message, response=response
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error archiving automation trigger")
        return Response.internal_server_error_response(_UNEXPECTED_ERROR_MESSAGE, response=response)
