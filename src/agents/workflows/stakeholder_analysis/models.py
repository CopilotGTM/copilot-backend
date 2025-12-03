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
        description="Primary motivations driving this stakeholder's interest in the solution",
    )
    pain_points: list[str] = Field(
        ...,
        description="List of pain points or challenges mentioned by this stakeholder",
    )
    keywords: list[str] = Field(
        ...,
        description="Key terms, phrases, or topics this stakeholder frequently references",
    )
    analysis_summary: str = Field(
        ...,
        description="A comprehensive narrative analysis of the stakeholder's role, influence, and engagement patterns",
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
