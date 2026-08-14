"""Authenticated catalog and materialisation routes for code-owned agent templates.

Catalog reads come directly from backend metadata and never open a tenant
session. The use action delegates to the normal agent-definition service so
ownership, validation, transaction handling, and future triggers remain on the
materialised user agent.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from fastapi import status as http_status
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.routers.agents.definitions_handler import get_agent_definition_service
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.contracts.agent_models import (
    AgentDetail,
    AgentTemplateDetail,
    AgentTemplateListResponse,
    UseAgentTemplateRequest,
)
from src.infra.utils.logger import get_logger
from src.service.agents.definition_service import AgentDefinitionService
from src.service.agents.errors import AgentServiceError
from src.service.agents.template_metadata import (
    agent_template_summary,
    get_current_agent_template,
)
from src.service.agents.template_metadata import (
    list_agent_templates as list_template_metadata,
)

logger = get_logger("agent_templates_handler")

_UNEXPECTED_ERROR_MESSAGE = "An unexpected error occurred while processing your request"

agent_templates_router = APIRouter()


@agent_templates_router.get(
    "",
    response_model=GenericResponse[AgentTemplateListResponse],
    summary="List agent templates",
    description="List code-owned base agents available to the authenticated user.",
)
async def list_agent_templates(
    _user_ids: UserIDs = Depends(get_current_user),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentTemplateListResponse]:
    """Return the small in-process catalog without touching tenant persistence."""

    templates = list_template_metadata()
    return Response.success_response(
        "Agent templates retrieved successfully",
        data=AgentTemplateListResponse(
            items=[agent_template_summary(template) for template in templates],
            total_count=len(templates),
        ),
    )


@agent_templates_router.get(
    "/{template_id}",
    response_model=GenericResponse[AgentTemplateDetail],
    summary="Get agent template",
    description="Return the current complete definition of one code-owned base agent.",
    responses={
        http_status.HTTP_404_NOT_FOUND: {"description": "Agent template not found."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
    },
)
async def get_agent_template_detail(
    template_id: UUID,
    response: FastAPIResponse,
    _user_ids: UserIDs = Depends(get_current_user),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentTemplateDetail]:
    """Return one current template while keeping arbitrary UUIDs non-discoverable."""

    template = get_current_agent_template(template_id)
    if template is None:
        return Response.error_response(
            status_code=http_status.HTTP_404_NOT_FOUND,
            message="Agent template not found",
            response=response,
        )
    return Response.success_response("Agent template retrieved successfully", data=template)


@agent_templates_router.post(
    "/{template_id}/use",
    response_model=GenericResponse[AgentDetail],
    status_code=http_status.HTTP_201_CREATED,
    summary="Use agent template",
    description="Copy one exact code-owned template version into the user's agents.",
    responses={
        http_status.HTTP_201_CREATED: {
            "description": "Agent created from template.",
            "headers": {
                "Location": {
                    "description": "Canonical URI of the materialised user agent.",
                    "schema": {"type": "string"},
                }
            },
        },
        http_status.HTTP_404_NOT_FOUND: {"description": "Agent template not found."},
        http_status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit exceeded."},
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Unexpected server error."},
    },
)
async def use_agent_template(
    template_id: UUID,
    body: UseAgentTemplateRequest,
    response: FastAPIResponse,
    service: AgentDefinitionService = Depends(get_agent_definition_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AgentDetail]:
    """Materialise one template and commit it through the existing agent transaction."""

    try:
        item = await service.use_template(template_id, version=body.version)
        await service.session.commit()
        response.headers["Location"] = f"/users/v1/agents/{item.agent_id}"
        return Response.created_response(
            "Agent template added successfully",
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
        logger.exception("Unexpected error using agent template")
        return Response.internal_server_error_response(
            _UNEXPECTED_ERROR_MESSAGE,
            response=response,
        )
