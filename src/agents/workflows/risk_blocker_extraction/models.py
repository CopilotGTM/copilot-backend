"""Models for risk/blocker extraction agent output.

Contains structured schemas used by the extraction agent for LLM responses.
"""

from pydantic import BaseModel, Field

from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerType,
)


class ExtractedRiskBlocker(BaseModel):
    """A single extracted risk or blocker from content analysis.

    This schema represents an AI-identified issue that may impact
    deal success. The agent extracts these from meeting transcripts,
    emails, and other communication sources.

    **RISK vs BLOCKER Classification (B2B Sales Industry Standard)**:

    RISK (potential issue, probability-based):
    - May occur and impact the deal negatively
    - Deal can still move forward, but needs monitoring
    - Examples: "need to check with boss", "also looking at competitors",
      "budget not approved yet", "CTO is skeptical", "not top priority"

    BLOCKER (active impediment, certainty-based):
    - IS currently preventing deal progress
    - Deal CANNOT move forward until resolved
    - Examples: "locked into competitor contract", "not authorized to sign",
      "legal freeze on new vendors", "budget exhausted", "champion left company"

    Enum fields:
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
