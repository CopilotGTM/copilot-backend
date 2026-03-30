"""Get stakeholder details tool for agents.

Tool for retrieving detailed information about a specific stakeholder.

SECURITY: organisation_id is taken from context (immutable).
This tool does NOT accept organisation_id as a parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


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

    # Parse stakeholder_id
    try:
        parsed_stakeholder_id = UUID(stakeholder_id)
    except ValueError:
        logger.warning(f"get_stakeholder_details: invalid stakeholder_id format: {stakeholder_id}")
        return None

    # Use table columns for SQLAlchemy operations (type-safe pattern)
    c = cast(Any, DealStakeholder).__table__.c

    stmt = select(DealStakeholder).where(
        c.organisation_id == organisation_id,
        c.stakeholder_id == parsed_stakeholder_id,
    )

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        try:
            result = await session.execute(stmt)
            stakeholder = result.scalar_one_or_none()
        except Exception as e:
            logger.exception(f"get_stakeholder_details: database error: {e}")
            return None

        if not stakeholder:
            logger.info(f"get_stakeholder_details: stakeholder not found: {stakeholder_id}")
            return None

        # Transform to output format with full details (while session is active)
        output: dict[str, Any] = {
            "stakeholder_id": str(stakeholder.stakeholder_id),
            "deal_id": str(stakeholder.deal_id),
            "email": stakeholder.email,
            "name": stakeholder.name,
            "job_title": stakeholder.job_title,
            "phone_number": stakeholder.phone_number,
            "department": stakeholder.department,
            "stance": stakeholder.stance.value
            if isinstance(stakeholder.stance, StakeholderStance)
            else stakeholder.stance,
            "decision_power": stakeholder.decision_power.value
            if isinstance(stakeholder.decision_power, DecisionPower)
            else stakeholder.decision_power,
            "champion_potential": stakeholder.champion_potential,
            "motivation": stakeholder.motivation,
            "pain_points": stakeholder.pain_points or [],
            "keywords": stakeholder.keywords or [],
            "temperature": stakeholder.temperature.value
            if isinstance(stakeholder.temperature, StakeholderTemperature)
            else stakeholder.temperature,
            "engagement_score": stakeholder.engagement_score,
            "last_contact_at": stakeholder.last_contact_at.isoformat()
            if stakeholder.last_contact_at
            else None,
            "external_links": stakeholder.external_links or [],
            "contact_id": str(stakeholder.contact_id) if stakeholder.contact_id else None,
            "source": stakeholder.source.value
            if isinstance(stakeholder.source, StakeholderSource)
            else stakeholder.source,
            "stakeholder_research": stakeholder.stakeholder_research,
            "last_researched_at": stakeholder.last_researched_at.isoformat()
            if stakeholder.last_researched_at
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
