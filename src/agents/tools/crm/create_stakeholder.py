"""Create stakeholder tool for deal-level agents.

Tool for creating a new stakeholder for a deal if one doesn't already exist.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Prevents cross-deal data access (deal-scoped tool)

This is a DEAL-SCOPED tool. It does NOT accept organisation_id or deal_id
as parameters - both come from context.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select

from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
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


# Type aliases for valid enum values (helps agents understand valid inputs)
StanceType = Literal["UNKNOWN", "CHAMPION", "SUPPORTER", "NEUTRAL", "DETRACTOR", "BLOCKER"]
DecisionPowerType = Literal[
    "UNKNOWN", "DECISION_MAKER", "INFLUENCER", "EVALUATOR", "GATEKEEPER", "END_USER"
]
TemperatureType = Literal["UNKNOWN", "HOT", "WARM", "COLD"]


class StakeholderInput(BaseModel):
    """Input model for creating a stakeholder.

    All fields except email have sensible defaults. The agent should provide
    as much information as available from the conversation/context.
    """

    email: Annotated[str, Field(description="Email address (required, used for deduplication)")]

    name: Annotated[str, Field(default="", description="Full name (e.g., 'John Smith')")]

    job_title: Annotated[
        str, Field(default="", description="Job title (e.g., 'VP of Engineering', 'CTO')")
    ]

    department: Annotated[
        str,
        Field(default="", description="Department (e.g., 'Engineering', 'Sales', 'Legal')"),
    ]

    stance: Annotated[
        StanceType,
        Field(
            default="UNKNOWN",
            description=(
                "Stakeholder stance: UNKNOWN (default), CHAMPION (strong advocate), "
                "SUPPORTER (positive), NEUTRAL (no preference), DETRACTOR (negative), "
                "BLOCKER (actively opposing)"
            ),
        ),
    ]

    decision_power: Annotated[
        DecisionPowerType,
        Field(
            default="UNKNOWN",
            description=(
                "Decision authority: UNKNOWN (default), DECISION_MAKER (final authority), "
                "INFLUENCER (influences decision), EVALUATOR (evaluates/recommends), "
                "GATEKEEPER (controls access), END_USER (will use product)"
            ),
        ),
    ]

    temperature: Annotated[
        TemperatureType,
        Field(
            default="UNKNOWN",
            description=(
                "Engagement level: UNKNOWN (default), HOT (very engaged), "
                "WARM (moderately engaged), COLD (low engagement)"
            ),
        ),
    ]

    motivation: Annotated[
        str,
        Field(default="", description="What motivates them (e.g., 'improve team efficiency')"),
    ]

    pain_points: Annotated[
        list[str],
        Field(default_factory=list, description="Pain points mentioned (e.g., ['slow processes'])"),
    ]

    champion_potential: Annotated[
        float,
        Field(default=0.0, ge=0.0, le=100.0, description="Champion potential score 0-100"),
    ]

    engagement_score: Annotated[
        float,
        Field(default=0.0, ge=0.0, le=100.0, description="Engagement score 0-100"),
    ]


def _parse_stance(value: str) -> StakeholderStance:
    """Parse stance string to enum."""
    try:
        return StakeholderStance(value.upper())
    except ValueError:
        return StakeholderStance.UNKNOWN


def _parse_decision_power(value: str) -> DecisionPower:
    """Parse decision_power string to enum."""
    try:
        return DecisionPower(value.upper())
    except ValueError:
        return DecisionPower.UNKNOWN


def _parse_temperature(value: str) -> StakeholderTemperature:
    """Parse temperature string to enum."""
    try:
        return StakeholderTemperature(value.upper())
    except ValueError:
        return StakeholderTemperature.UNKNOWN


async def create_stakeholder(
    *,
    context: AgentContext,
    input_data: StakeholderInput,
) -> dict[str, Any] | None:
    """Create a new stakeholder for a deal, or return existing if found by email.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access

    Args:
        context: AgentContext containing organisation and deal metadata
        input_data: StakeholderInput with stakeholder details

    Returns:
        Dictionary with stakeholder details and created/existing status, or None on error.
    """
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("create_stakeholder: missing organisation_id in context")
        return None

    if not deal_id:
        logger.warning("create_stakeholder: missing deal_id in context")
        return None

    email = input_data.email.strip().lower()
    if not email:
        logger.warning("create_stakeholder: email is required")
        return None

    # Use table columns for SQLAlchemy operations
    c = cast(Any, DealStakeholder).__table__.c

    check_stmt = select(DealStakeholder).where(
        c.organisation_id == organisation_id,
        c.deal_id == deal_id,
        c.email == email,
    )

    async with context.acquire_session() as session:
        try:
            result = await session.execute(check_stmt)
            existing = result.scalar_one_or_none()

            if existing:
                logger.info(
                    "create_stakeholder: stakeholder exists for email=%s, deal=%s",
                    email,
                    deal_id,
                )
                return _format_stakeholder_output(existing, created=False)

            # Build stakeholder data
            stakeholder_data: dict[str, Any] = {
                "stakeholder_id": uuid4(),
                "organisation_id": organisation_id,
                "integration_id": None,
                "deal_id": deal_id,
                "contact_id": None,
                "email": email,
                "name": input_data.name.strip() or None,
                "job_title": input_data.job_title.strip() or None,
                "department": input_data.department.strip() or None,
                "stance": _parse_stance(input_data.stance),
                "decision_power": _parse_decision_power(input_data.decision_power),
                "temperature": _parse_temperature(input_data.temperature),
                "motivation": input_data.motivation.strip() or None,
                "pain_points": input_data.pain_points or None,
                "keywords": None,
                "champion_potential": input_data.champion_potential,
                "engagement_score": input_data.engagement_score,
                "external_links": None,
                "source": StakeholderSource.MANUAL,
                "source_reference_id": None,
                "approval_status": ApprovalStatus.APPROVED,
                "last_contact_at": None,
                "stakeholder_data": None,
                "stakeholder_engagement_history": None,
                "is_deleted": False,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }

            new_stakeholder = DealStakeholder.model_validate(stakeholder_data)
            session.add(new_stakeholder)
            await session.commit()
            await session.refresh(new_stakeholder)

            logger.info(
                "create_stakeholder: created stakeholder email=%s for deal=%s",
                email,
                deal_id,
            )
            return _format_stakeholder_output(new_stakeholder, created=True)

        except Exception as e:
            logger.exception(f"create_stakeholder: database error: {e}")
            await session.rollback()
            return None


def _format_stakeholder_output(stakeholder: DealStakeholder, created: bool) -> dict[str, Any]:
    """Format stakeholder record for output."""
    return {
        "stakeholder_id": str(stakeholder.stakeholder_id),
        "email": stakeholder.email,
        "name": stakeholder.name,
        "job_title": stakeholder.job_title,
        "department": stakeholder.department,
        "stance": (
            stakeholder.stance.value
            if isinstance(stakeholder.stance, StakeholderStance)
            else stakeholder.stance
        ),
        "decision_power": (
            stakeholder.decision_power.value
            if isinstance(stakeholder.decision_power, DecisionPower)
            else stakeholder.decision_power
        ),
        "temperature": (
            stakeholder.temperature.value
            if isinstance(stakeholder.temperature, StakeholderTemperature)
            else stakeholder.temperature
        ),
        "motivation": stakeholder.motivation,
        "pain_points": stakeholder.pain_points,
        "champion_potential": stakeholder.champion_potential,
        "engagement_score": stakeholder.engagement_score,
        "source": (
            stakeholder.source.value
            if isinstance(stakeholder.source, StakeholderSource)
            else stakeholder.source
        ),
        "created": created,
        "message": "Stakeholder created successfully" if created else "Stakeholder already exists",
    }


def create_create_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the create_stakeholder tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Prevents cross-deal data access
    """

    async def create_stakeholder_tool(
        email: str,
        name: str = "",
        job_title: str = "",
        department: str = "",
        stance: StanceType = "UNKNOWN",
        decision_power: DecisionPowerType = "UNKNOWN",
        temperature: TemperatureType = "UNKNOWN",
        motivation: str = "",
        pain_points: list[str] | None = None,
        champion_potential: float = 0.0,
        engagement_score: float = 0.0,
    ) -> dict[str, Any] | None:
        """Create a new stakeholder for the current deal if not already present.

        Use this tool when you identify a new person during analysis of meetings,
        emails, or transcripts who is not yet in the stakeholder list.

        IMPORTANT: First use `list_deal_stakeholders` to check if they already exist.

        Args:
            email: Email address (required, used for deduplication)
            name: Full name (e.g., "John Smith")
            job_title: Job title (e.g., "VP of Engineering", "CTO", "Product Manager")
            department: Department (e.g., "Engineering", "Sales", "Finance", "Legal")
            stance: Stakeholder stance toward your solution:
                - UNKNOWN: Not enough information to assess (default)
                - CHAMPION: Strong internal advocate pushing for your solution
                - SUPPORTER: Generally positive toward your solution
                - NEUTRAL: No clear preference or opinion
                - DETRACTOR: Negative or skeptical toward your solution
                - BLOCKER: Actively opposing or blocking the deal
            decision_power: Decision-making authority:
                - UNKNOWN: Not enough information to assess (default)
                - DECISION_MAKER: Has final authority to approve/reject
                - INFLUENCER: Can significantly influence the decision
                - EVALUATOR: Evaluates options and makes recommendations
                - GATEKEEPER: Controls access to decision makers
                - END_USER: Will use the product day-to-day
            temperature: Current engagement level:
                - UNKNOWN: Not enough information to assess (default)
                - HOT: Very engaged, responding quickly, actively participating
                - WARM: Moderately engaged, regular communication
                - COLD: Low engagement, slow responses, or unresponsive
            motivation: What motivates this person (e.g., "Wants to reduce manual work")
            pain_points: List of pain points they've mentioned (e.g., ["slow approval process"])
            champion_potential: Score 0-100 indicating likelihood of becoming a champion (default: 0)
            engagement_score: Score 0-100 based on interaction frequency/quality (default: 0)

        Returns:
            Dictionary with stakeholder details and whether they were created or already existed.

        Note: organisation_id and deal_id come from context automatically.
        """
        logger.info("[TOOL CALLED] create_stakeholder - email='%s', name='%s'", email, name)

        if not context.organisation_id:
            logger.warning("create_stakeholder unavailable: missing organisation_id")
            return None

        if not context.deal_id:
            logger.warning("create_stakeholder unavailable: missing deal_id")
            return None

        # Build input model
        input_data = StakeholderInput(
            email=email,
            name=name,
            job_title=job_title,
            department=department,
            stance=stance,
            decision_power=decision_power,
            temperature=temperature,
            motivation=motivation,
            pain_points=pain_points or [],
            champion_potential=champion_potential,
            engagement_score=engagement_score,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateStakeholder",
            tool_func=create_stakeholder,
            parameters={
                "context": context,
                "input_data": input_data,
            },
            description=f"Creating stakeholder: {email}",
            fallback_value=None,
        )

        if result:
            status = "created" if result.get("created") else "found existing"
            logger.info(
                "[TOOL RESULT] create_stakeholder %s: email=%s, id=%s",
                status,
                result.get("email"),
                result.get("stakeholder_id"),
            )
        else:
            logger.info("[TOOL RESULT] create_stakeholder failed")

        return dict(result) if result else None

    return create_stakeholder_tool
