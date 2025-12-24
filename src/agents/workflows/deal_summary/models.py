"""Models for deal summary agent input and output.

Contains structured schemas for the deal summary agent.
"""

from enum import Enum

from pydantic import BaseModel, Field


class DealSummaryInput(BaseModel):
    """Minimal input data for DealSummaryAgent.

    Instead of passing all data pre-loaded, we pass minimal context
    and let the agent use tools to gather what it needs.
    """

    deal_id: str = Field(..., description="UUID of the deal")
    deal_name: str = Field(..., description="Name of the deal")
    company_name: str | None = Field(None, description="Company name")
    deal_stage: str | None = Field(None, description="Current deal stage")
    deal_amount: float | None = Field(None, description="Deal amount")
    currency: str | None = Field(None, description="Currency code")
    close_date: str | None = Field(None, description="Expected close date")
    deal_status: str = Field(..., description="Deal status (open/won/lost)")

    # Summary counts to help agent plan its data gathering
    meeting_count: int = Field(0, description="Number of meetings for this deal")
    stakeholder_count: int = Field(0, description="Number of known stakeholders")
    email_count: int = Field(0, description="Number of linked email threads")

    # Date range to help agent understand timeline
    first_meeting_date: str | None = Field(None, description="Date of first meeting")
    last_meeting_date: str | None = Field(None, description="Date of most recent meeting")


class DealHealthStatus(str, Enum):
    """AI-assessed deal health classification.

    Categories based on observable signals and underlying failure patterns:
    - HEALTHY: Deal progressing with validated value and forward momentum
    - AT_RISK: Early warning signs emerging without a single blocking issue
    - STALLED: Deal stopped advancing without explicit rejection
    - CHAMPION_WEAK: No effective internal advocate driving the deal forward
    - BLOCKED: Deal stopped by a known, explicit external dependency
    - MISALIGNED: Stakeholders do not share a common definition of success
    - UNVALIDATED: Interest exists but value not proven in customer's context
    - COLD: Engagement dropped sharply with no active dialogue
    - LATE_STAGE_RISK: Deal appears close but exhibits fragility threatening closure
    - LIKELY_LOST: Deal trajectory strongly indicates loss despite no formal rejection
    - ARTIFICIALLY_HEALTHY: Surface metrics look healthy but lacks structural buying readiness
    """

    HEALTHY = "HEALTHY"
    AT_RISK = "AT_RISK"
    STALLED = "STALLED"
    CHAMPION_WEAK = "CHAMPION_WEAK"
    BLOCKED = "BLOCKED"
    MISALIGNED = "MISALIGNED"
    UNVALIDATED = "UNVALIDATED"
    COLD = "COLD"
    LATE_STAGE_RISK = "LATE_STAGE_RISK"
    LIKELY_LOST = "LIKELY_LOST"
    ARTIFICIALLY_HEALTHY = "ARTIFICIALLY_HEALTHY"


class DealSummaryData(BaseModel):
    """Generated deal summary data with health assessment."""

    deal_summary: str = Field(
        ...,
        description="Comprehensive narrative summary of the entire deal, synthesizing all meeting discussions, key decisions, stakeholder dynamics, and deal progression.",
    )

    deal_health: DealHealthStatus = Field(
        ...,
        description="AI-assessed health status: HEALTHY, AT_RISK, STALLED, CHAMPION_WEAK, BLOCKED, MISALIGNED, UNVALIDATED, COLD, LATE_STAGE_RISK, LIKELY_LOST, or ARTIFICIALLY_HEALTHY.",
    )

    deal_health_reason: str = Field(
        ...,
        description="Brief explanation (2-3 sentences) for the assigned deal health status, citing engagement, risks, or blockers.",
    )
