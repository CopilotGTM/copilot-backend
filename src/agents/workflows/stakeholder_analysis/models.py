"""Models for stakeholder analysis agent output.

Contains structured output schemas used by the stakeholder analysis agent for LLM responses.
"""

from pydantic import BaseModel, Field

from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
    StakeholderTemperature,
)


class StakeholderAnalysisData(BaseModel):
    """Generated stakeholder analysis data from meeting transcripts.

    This schema is used by the stakeholder analysis agent to extract
    insights about a stakeholder from their meeting interactions.
    """

    stakeholder_email: str = Field(
        ...,
        description="Email address of the stakeholder being analyzed (used to match results)",
    )
    stance: StakeholderStance
    decision_power: DecisionPower
    temperature: StakeholderTemperature
    champion_potential: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="A 0-100 score indicating the potential for this stakeholder to become a champion for the deal",
    )
    motivation: str = Field(
        ...,
        description=(
            "One concise seller-facing statement describing the strongest outcome, pressure, "
            "or personal win this stakeholder cares about"
        ),
    )
    pain_points: list[str] = Field(
        ...,
        min_length=2,
        max_length=3,
        description=(
            "Exactly 2 or 3 stakeholder-specific problems, blockers, or frustrations that "
            "would materially increase this person's interest if solved"
        ),
    )
    keywords: list[str] = Field(
        ...,
        description="Key terms, phrases, or topics this stakeholder frequently references",
    )
    analysis_summary: str = Field(
        ...,
        description=(
            "A concise seller-ready analysis of the stakeholder's role, influence, engagement "
            "pattern, and how to work with them"
        ),
    )


class DealStakeholdersAnalysisData(BaseModel):
    """Batch analysis results for all stakeholders in a deal.

    This schema is used by the stakeholder analysis agent to analyze
    all stakeholders for a deal in a single LLM call.
    """

    stakeholders: list[StakeholderAnalysisData] = Field(
        ...,
        description="List of analysis results for each stakeholder in the deal",
    )
