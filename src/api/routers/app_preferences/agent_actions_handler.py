"""User-facing API routes for agent action approval preferences.

Called by the app settings surface to show which agent write actions can run
directly and which require confirmation. The router only exposes the current
user's preferences through the authenticated master-session dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.agent_action_approval_models import (
    AgentActionApprovalPreferencesResponse,
    UpdateAgentActionApprovalPreferencesRequest,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.service.agentic_approvals.action_approval_service import AgentActionApprovalService

agent_action_approval_router = APIRouter()


@agent_action_approval_router.get(
    "/agent-actions",
    response_model=GenericResponse[AgentActionApprovalPreferencesResponse],
)
async def get_agent_action_approval_preferences(
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentActionApprovalPreferencesResponse]:
    """Return the effective approval settings for the authenticated user.

    Defaults, locked tools, and saved user preferences are merged in the service
    so the API response is the exact policy shape the agent tools will evaluate.
    """
    service = AgentActionApprovalService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
    )
    payload = await service.get_preferences()
    return Response.success_response(
        message="Agent action approval preferences retrieved successfully",
        data=payload,
    )


@agent_action_approval_router.patch(
    "/agent-actions",
    response_model=GenericResponse[AgentActionApprovalPreferencesResponse],
)
async def update_agent_action_approval_preferences(
    request: UpdateAgentActionApprovalPreferencesRequest,
    user_ids: UserIDs = Depends(get_current_user),
    master_session: AsyncSession = Depends(get_master_async_session),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[AgentActionApprovalPreferencesResponse]:
    """Persist editable approval settings for the authenticated user.

    Locked tools are intentionally ignored by the service merge step; clients can
    send the full visible settings object without weakening mandatory approvals.
    """
    service = AgentActionApprovalService(
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
    )
    payload = await service.update_preferences(request)
    return Response.success_response(
        message="Agent action approval preferences updated successfully",
        data=payload,
    )
