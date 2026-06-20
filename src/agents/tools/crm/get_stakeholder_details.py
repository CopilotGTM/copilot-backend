"""Get stakeholder details tool for agents.

Tool for retrieving detailed information about a specific stakeholder.

SECURITY: organisation_id is taken from context (immutable).
This tool does NOT accept organisation_id as a parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.agents.tools.base import execute_with_capabilities
from src.service.crm.participants.stakeholder_read_service import StakeholderReadService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


async def get_stakeholder_details(
    stakeholder_id: str,
    *,
    context: AgentContext,
) -> dict[str, Any] | None:
    """Get detailed information about a specific stakeholder.

    Returns comprehensive stakeholder information including AI-extracted
    insights like motivation, pain points, and keywords.

    SECURITY: organisation_id is taken from context (immutable).
    This prevents cross-org data access.

    Args:
        stakeholder_id: The stakeholder UUID as a string
        context: AgentContext containing organisation metadata

    Returns:
        Stakeholder dictionary with detailed information, or None if not found.
    """
    # SECURITY: organisation_id comes from context, not parameters
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("get_stakeholder_details: missing organisation_id in context")
        return None

    actual_deal_id = getattr(context, "deal_id", None)
    if not actual_deal_id:
        logger.warning("get_stakeholder_details: no deal_id in context")
        return None

    # Parse stakeholder_id
    try:
        parsed_stakeholder_id = UUID(stakeholder_id)
    except ValueError:
        logger.warning(f"get_stakeholder_details: invalid stakeholder_id format: {stakeholder_id}")
        return None

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            service = StakeholderReadService(
                session,
                organisation_id,
                getattr(context, "user_id", None),
            )
            payload = await service.get_stakeholder_detail(actual_deal_id, parsed_stakeholder_id)
        except Exception as e:
            logger.exception(f"get_stakeholder_details: database error: {e}")
            return None

        if not payload:
            logger.info(f"get_stakeholder_details: stakeholder not found: {stakeholder_id}")
            return None

        output: dict[str, Any] = {
            "participant_id": str(payload["participant_id"]),
            "deal_id": str(payload["deal_id"]),
            "email": payload["email"],
            "name": payload["name"],
            "job_title": payload["job_title"],
            "phone_number": payload["phone_number"],
            "department": payload["department"],
            "stance": _enum_value(payload["stance"]),
            "decision_power": _enum_value(payload["decision_power"]),
            "motivation": payload["motivation"],
            "pain_points": payload["pain_points"] or [],
            "keywords": payload["keywords"] or [],
            "temperature": _enum_value(payload["temperature"]),
            "last_contact_at": payload["last_contact_at"].isoformat()
            if payload["last_contact_at"]
            else None,
            "external_links": payload["external_links"] or [],
            "contact_id": str(payload["contact_id"]) if payload["contact_id"] else None,
            "source": _enum_value(payload["source"]),
            "stakeholder_research": payload["stakeholder_research"],
            "last_researched_at": payload["last_researched_at"].isoformat()
            if payload["last_researched_at"]
            else None,
        }
    # Session released

    logger.info(f"get_stakeholder_details: found stakeholder {stakeholder_id}")
    return output


def create_get_stakeholder_details(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_stakeholder_details tool bound to a context.

    SECURITY: The context contains the immutable organisation_id.
    The tool does NOT accept organisation_id as a parameter.
    """

    async def get_stakeholder_details_tool(
        stakeholder_id: str,
    ) -> dict[str, Any] | None:
        """Get detailed information about a specific stakeholder.

        Use this tool when you need comprehensive information about a person
        involved in a deal, including their motivations, pain points, stance,
        and engagement level.

        Args:
            stakeholder_id: The stakeholder UUID to get details for

        Note: organisation_id is automatically set from context and cannot be overridden.
        """
        logger.info(f"[TOOL CALLED] get_stakeholder_details - stakeholder_id='{stakeholder_id}'")

        if not context.organisation_id:
            logger.warning(
                "get_stakeholder_details unavailable: missing organisation_id in context"
            )
            return None

        # Tool acquires its own sessions on-demand, no lock needed
        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetStakeholderDetails",
            tool_func=get_stakeholder_details,
            parameters={
                "stakeholder_id": stakeholder_id,
                "context": context,
            },
            description="Checking stakeholder details",
            fallback_value=None,
        )

        if result:
            logger.info(
                f"[TOOL RESULT] get_stakeholder_details found stakeholder: {result.get('name', 'Unknown')}"
            )
        else:
            logger.info("[TOOL RESULT] get_stakeholder_details returned None (not found)")

        return dict(result) if result else None

    return get_stakeholder_details_tool
