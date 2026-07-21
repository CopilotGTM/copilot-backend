"""Read-only capability discovery for the user-agent editor."""

from fastapi import APIRouter, Depends
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.agent_models import AgentCapabilityCatalog
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.service.agents.capability_catalog import build_agent_capability_catalog

agent_capabilities_router = APIRouter()


@agent_capabilities_router.get(
    "/agent-capability-catalog",
    response_model=GenericResponse[AgentCapabilityCatalog],
    summary="List agent capabilities",
    description="List code-owned tools and skills selectable by the authenticated user.",
)
async def get_agent_capability_catalog(
    _user_ids: UserIDs = Depends(get_current_user),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[AgentCapabilityCatalog]:
    """Return capability metadata without opening a tenant database session."""

    return Response.success_response(
        "Agent capabilities retrieved successfully",
        data=build_agent_capability_catalog(),
    )
