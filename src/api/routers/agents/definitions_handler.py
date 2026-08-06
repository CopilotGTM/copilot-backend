"""Versioned CRUD endpoints for private user-owned agent definitions.

This router binds immutable organisation and owner identity from authentication, applies rate
limits, and owns transaction completion. Agent configuration rules and persistence remain in
the service package so future non-HTTP callers use the same security boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from src.api.schemas.agent_models import (
    AgentCapabilitiesUpdateRequest,
    AgentCreateRequest,
    AgentDetail,
    AgentListResponse,
    AgentUpdateRequest,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.master_tables import UserAppRole
from src.domain.models.organisation_tables import AgentStatus
from src.infra.utils.logger import get_logger
from src.service.agents.definition_service import AgentDefinitionService
from src.service.agents.errors import AgentServiceError

logger = get_logger("agent_definitions_handler")

_UNEXPECTED_ERROR_MESSAGE = "An unexpected error occurred while processing your request"

agent_definitions_router = APIRouter()


async def get_agent_definition_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
) -> AsyncIterator[AgentDefinitionService]:
    """Bind the service to authenticated tenant and owner identity for one request."""

    service = AgentDefinitionService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        is_admin=user_ids.user_role == UserAppRole.ADMIN,
    )
    try:
        yield service
    finally:
        await service.close()


@agent_definitions_router.post(
    "",
    response_model=GenericResponse[AgentDetail],
    status_code=http_status.HTTP_201_CREATED,
    summary="Create agent definition",
    description="Create a private executable agent owned by the authenticated user.",
    responses={
        http_status.HTTP_201_CREATED: {
            "description": "Agent created successfully.",
            "headers": {
                "Location": {
                    "description": "Canonical URI of the created agent.",
                    "schema": {"type": "string"},
                }
            },
        },
        http_status.HTTP_400_BAD_REQUEST: {"description": "Invalid agent definition."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Unexpected server error."},
    },
)
async def create_agent_definition(
    body: AgentCreateRequest,
    response: FastAPIResponse,
    service: AgentDefinitionService = Depends(get_agent_definition_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AgentDetail]:
    """Create one validated definition and commit it as a single request transaction."""

    try:
        item = await service.create_agent(body)
        await service.session.commit()
        response.headers["Location"] = f"/users/v1/agents/{item.agent_id}"
        return Response.created_response(
            "Agent created successfully",
            data=item,
            response=response,
        )
    except AgentServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error creating agent definition")
        return Response.internal_server_error_response(
            _UNEXPECTED_ERROR_MESSAGE,
            response=response,
        )


@agent_definitions_router.get(
    "",
    response_model=GenericResponse[AgentListResponse],
    summary="List agent definitions",
    description="List active or paused agents owned by the authenticated user.",
    responses={
        http_status.HTTP_400_BAD_REQUEST: {"description": "Invalid list filter."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Unexpected server error."},
    },
)
async def list_agent_definitions(
    response: FastAPIResponse,
    status: AgentStatus | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: AgentDefinitionService = Depends(get_agent_definition_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentListResponse]:
    """Return an owner-scoped offset page without completing a write transaction."""

    try:
        items = await service.list_agents(
            status=status,
            limit=limit,
            offset=offset,
        )
        return Response.success_response("Agents retrieved successfully", data=items)
    except AgentServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error listing agent definitions")
        return Response.internal_server_error_response(
            _UNEXPECTED_ERROR_MESSAGE,
            response=response,
        )


@agent_definitions_router.get(
    "/{agent_id}",
    response_model=GenericResponse[AgentDetail],
    summary="Get agent definition",
    description="Return one non-archived agent owned by the authenticated user.",
    responses={
        http_status.HTTP_404_NOT_FOUND: {"description": "Agent not found."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Unexpected server error."},
    },
)
async def get_agent_definition(
    agent_id: UUID,
    response: FastAPIResponse,
    service: AgentDefinitionService = Depends(get_agent_definition_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentDetail]:
    """Return one complete definition without exposing foreign owner identifiers."""

    try:
        item = await service.get_agent(agent_id)
        return Response.success_response("Agent retrieved successfully", data=item)
    except AgentServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error retrieving agent definition")
        return Response.internal_server_error_response(
            _UNEXPECTED_ERROR_MESSAGE,
            response=response,
        )


@agent_definitions_router.put(
    "/{agent_id}",
    response_model=GenericResponse[AgentDetail],
    summary="Replace agent definition",
    description="Replace all mutable fields when expected_revision matches the stored revision.",
    responses={
        http_status.HTTP_400_BAD_REQUEST: {"description": "Invalid agent definition."},
        http_status.HTTP_404_NOT_FOUND: {"description": "Agent not found."},
        http_status.HTTP_409_CONFLICT: {"description": "Revision conflict."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Unexpected server error."},
    },
)
async def replace_agent_definition(
    agent_id: UUID,
    body: AgentUpdateRequest,
    response: FastAPIResponse,
    service: AgentDefinitionService = Depends(get_agent_definition_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AgentDetail]:
    """Atomically replace a definition and commit only after revision validation succeeds."""

    try:
        item = await service.replace_agent(agent_id, body)
        await service.session.commit()
        return Response.success_response("Agent updated successfully", data=item)
    except AgentServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error replacing agent definition")
        return Response.internal_server_error_response(
            _UNEXPECTED_ERROR_MESSAGE,
            response=response,
        )


@agent_definitions_router.put(
    "/{agent_id}/capabilities",
    response_model=GenericResponse[AgentDetail],
    summary="Replace agent capabilities",
    description="Atomically replace selected tools and skills at an expected agent revision.",
    responses={
        http_status.HTTP_400_BAD_REQUEST: {"description": "Invalid capability selection."},
        http_status.HTTP_404_NOT_FOUND: {"description": "Agent not found."},
        http_status.HTTP_409_CONFLICT: {"description": "Revision conflict."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Unexpected server error."},
    },
)
async def replace_agent_capabilities(
    agent_id: UUID,
    body: AgentCapabilitiesUpdateRequest,
    response: FastAPIResponse,
    service: AgentDefinitionService = Depends(get_agent_definition_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AgentDetail]:
    """Commit one complete capability set after revision-aware validation."""

    try:
        item = await service.replace_capabilities(agent_id, body)
        await service.session.commit()
        return Response.success_response("Agent capabilities updated successfully", data=item)
    except AgentServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error replacing agent capabilities")
        return Response.internal_server_error_response(
            _UNEXPECTED_ERROR_MESSAGE,
            response=response,
        )


@agent_definitions_router.delete(
    "/{agent_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Archive agent definition",
    description="Soft-delete an owned agent when expected_revision matches.",
    responses={
        http_status.HTTP_404_NOT_FOUND: {"description": "Agent not found."},
        http_status.HTTP_409_CONFLICT: {"description": "Revision conflict."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Unexpected server error."},
    },
)
async def archive_agent_definition(
    agent_id: UUID,
    response: FastAPIResponse,
    expected_revision: int = Query(ge=1),
    service: AgentDefinitionService = Depends(get_agent_definition_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> FastAPIResponse | GenericResponse[None]:
    """Archive one definition without deleting history-bearing database rows."""

    try:
        await service.archive_agent(agent_id, expected_revision=expected_revision)
        await service.session.commit()
        return Response.no_content_response()
    except AgentServiceError as exc:
        await service.session.rollback()
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        await service.session.rollback()
        logger.exception("Unexpected error archiving agent definition")
        return Response.internal_server_error_response(
            _UNEXPECTED_ERROR_MESSAGE,
            response=response,
        )
