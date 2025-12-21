"""Models for risk/blocker extraction agent input and output.

Contains structured schemas used by the extraction agent for LLM responses.
"""

from pydantic import BaseModel, Field

from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerType,
)


class RiskBlockerExtractionInput(BaseModel):
    """Minimal input data for RiskBlockerExtractionAgent.

    Instead of passing all content pre-loaded, we pass minimal context
    and let the agent use tools to gather what it needs.
    """

    deal_id: str = Field(..., description="UUID of the deal")
    deal_name: str = Field(..., description="Name of the deal")
    company_name: str | None = Field(None, description="Company name")
    deal_stage: str | None = Field(None, description="Current deal stage")

    # Counts to help agent plan its data gathering
    meeting_count: int = Field(0, description="Number of meetings for this deal")
    email_count: int = Field(0, description="Number of linked email threads")
    existing_risks_count: int = Field(0, description="Number of existing risks")
    existing_blockers_count: int = Field(0, description="Number of existing blockers")

    # Date range to help agent understand timeline
    first_meeting_date: str | None = Field(None, description="Date of first meeting")
    last_meeting_date: str | None = Field(None, description="Date of most recent meeting")


class ExtractedRiskBlocker(BaseModel):
    """A single extracted risk or blocker from content analysis.

    This schema represents an AI-identified issue that may impact
    deal success. The agent extracts these from meeting transcripts,
    emails, and other communication sources.

    Enum fields (item_type, category, severity, source_type):
    - item_type: RISK (potential issue) or BLOCKER (active impediment)
    - category: TECHNICAL, BUDGET, TIMELINE, COMPETITION, STAKEHOLDER, etc.
    - severity: CRITICAL, HIGH, MEDIUM, or LOW
    - source_type: EMAIL_GMAIL, EMAIL_OUTLOOK, MEETING_GOOGLE, TEAMS_MEETING, ZOOM_MEETING, SLACK, etc.
    """

    # Enum fields - no description allowed (OpenAI structured output limitation with $ref)
    item_type: RiskBlockerType
    category: RiskBlockerCategory
    severity: RiskBlockerSeverity

    title: str = Field(
        ...,
        max_length=500,
        description="Short, descriptive title for the risk/blocker (max 500 chars)",
    )
    description: str = Field(
        ...,
        description="Detailed explanation of the risk/blocker and its potential impact",
    )
    quote: str | None = Field(
        None,
        description="Direct quote from the source content that supports this identification",
    )
    reasoning: str = Field(
        ...,
        description="Explanation of why this was identified as a risk/blocker",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score from 0.0 to 1.0 indicating certainty of identification",
    )
    source_context: str = Field(
        ...,
        description="Brief context about where this was identified (e.g., 'Q3 Planning Call', 'Email from CTO')",
    )
    # Enum field - no description (OpenAI $ref limitation)
    source_type: RiskBlockerSource

    is_duplicate: bool = Field(
        False,
        description="Whether this appears to duplicate an existing risk/blocker",
    )
    duplicate_of_id: str | None = Field(
        None,
        description="If is_duplicate=true, the ID of the existing item this duplicates",
    )
    suggested_owner: str | None = Field(
        None,
        description="Suggested person or team who should own addressing this issue",
    )


class RiskBlockerExtractionResult(BaseModel):
    """Complete extraction results for a content analysis run.

    This schema contains all extracted risks and blockers from
    analyzing one or more content sources for a deal.
    """

    items: list[ExtractedRiskBlocker] = Field(
        default_factory=list,
        description="List of extracted risks and blockers",
    )
    sources_analyzed: list[str] = Field(
        default_factory=list,
        description="List of source references that were analyzed",
    )
    total_risks: int = Field(
        0,
        description="Count of items identified as risks",
    )
    total_blockers: int = Field(
        0,
        description="Count of items identified as blockers",
    )
    new_items_count: int = Field(
        0,
        description="Count of items that are NOT duplicates of existing items",
    )
    duplicate_items_count: int = Field(
        0,
        description="Count of items that duplicate existing items",
    )
    analysis_summary: str = Field(
        "",
        description="High-level summary of the extraction analysis",
    )
