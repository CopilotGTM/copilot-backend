"""Models for deal summary agent input and output.

Contains structured schemas for the deal summary agent.
"""

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


class DealSummaryData(BaseModel):
    """Generated deal summary data.

    Aggregates insights from all meetings associated with a deal,
    providing a comprehensive overview of the deal's history and status.
    """

    deal_summary: str = Field(
        ...,
        description="Comprehensive narrative summary of the entire deal, synthesizing all meeting discussions, key decisions, stakeholder dynamics, and deal progression.",
    )
