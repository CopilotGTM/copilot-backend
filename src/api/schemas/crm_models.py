"""CRM API response models for deals and related entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)


class DealTeamMember(BaseModel):
    """A team member associated with a deal (owner, collaborator, split_owner, etc.)."""

    external_id: str = Field(description="CRM external ID of the user")
    name: str | None = Field(None, description="Display name of the team member")
    email: str | None = Field(None, description="Email address of the team member")
    role: str = Field(description="Role in the deal: owner, collaborator, split_owner, etc.")

    model_config = {"from_attributes": True}


class DealOwnerDetail(BaseModel):
    """Owner details for a deal.

    This is exposed as a list in list responses for forward compatibility
    (e.g., multiple owners in some CRMs).
    """

    external_id: str = Field(description="CRM external ID of the owner")
    name: str | None = Field(None, description="Display name of the owner")
    email: str | None = Field(None, description="Email address of the owner")

    model_config = {"from_attributes": True}


class DealListItem(BaseModel):
    """Individual deal item in a list response."""

    deal_id: UUID
    external_id: str
    name: str | None
    amount: float | None
    currency: str | None
    deal_status: str
    probability: float | None
    close_date: datetime | None
    stage_name: str | None
    stage_label: str | None
    pipeline_name: str | None
    pipeline_label: str | None
    owner_details: list[DealOwnerDetail] = Field(
        default_factory=list,
        description="Owner details as a list (typically 0..1 items).",
    )
    company_external_id: str | None
    company_name: str | None = None
    company_domain: str | None = None
    deal_health: str | None = Field(
        None,
        description="AI-assessed health for the deal: ON_TRACK, NEEDS_ATTENTION, AT_RISK, CRITICAL, or STALLED.",
    )
    deal_health_reason: str | None = Field(
        None,
        description="Brief explanation for the AI-assessed deal health.",
    )
    deal_team: list[DealTeamMember] = Field(
        default_factory=list,
        description="List of team members (owners, collaborators, split_owners) associated with the deal.",
    )
    is_internal: bool = Field(
        ...,
        description="True if this is an internal CRM deal (editable), False if from external CRM (read-only).",
    )
    created_at: datetime
    updated_at: datetime
    synced_at: datetime

    model_config = {"from_attributes": True}


class DealsPage(BaseModel):
    """Paginated list of deals."""

    items: list[DealListItem]
    cursor: str | None = Field(None, description="Cursor for next page; null if no more results")


class DealDetail(BaseModel):
    """Detailed deal information - streamlined for frontend needs."""

    # Identity
    deal_id: UUID
    external_id: str
    name: str | None

    # Deal value
    amount: float | None
    currency: str | None

    # Status & Stage
    deal_status: str
    stage_name: str | None
    stage_label: str | None

    # Timeline (frontend: Creation Date, Close Date, Deal Age, Last Communication)
    source_created_at: datetime | None
    close_date: datetime | None
    last_activity_at: datetime | None
    deal_age_days: int | None = Field(None, description="Days since deal was created")

    # Owner & Team
    owner_details: list[DealOwnerDetail] = Field(
        default_factory=list,
        description="Owner details as a list (typically 0..1 items).",
    )
    deal_team: list[DealTeamMember] = Field(
        default_factory=list,
        description="List of team members associated with the deal.",
    )

    # Company/Industry
    company_name: str | None = None
    company_industry: str | None = Field(None, description="Industry of the associated company")

    # Deal health assessment
    deal_health: str | None = Field(
        None,
        description="AI-assessed health: ON_TRACK, NEEDS_ATTENTION, AT_RISK, CRITICAL, or STALLED.",
    )
    deal_health_reason: str | None = Field(
        None,
        description="Brief explanation for the AI-assessed deal health.",
    )

    # CRM source indicator
    is_internal: bool = Field(
        ...,
        description="True if this is an internal CRM deal (editable), False if from external CRM (read-only).",
    )

    # Time tracked (placeholder for future implementation)
    time_tracked: str | None = Field(None, description="Total time tracked on this deal")

    # Deal Summary (AI-generated)
    deal_summary_data: dict[str, Any] | None = Field(
        None,
        description="AI-generated deal summary with schema_version, generated_at, and data",
    )

    # Active deal-level jobs (for SSE subscription)
    active_deal_jobs: DealSummaryJobsStatus | None = Field(
        None,
        description="Currently running deal-level jobs (for SSE reconnection)",
    )

    model_config = {"from_attributes": True}


class DealsStats(BaseModel):
    """Aggregate statistics for deals."""

    total: int
    by_status: dict[str, int]
    by_pipeline: dict[str, int]
    total_amount: float | None = Field(None, description="Sum of all deal amounts")
    average_amount: float | None = Field(None, description="Average deal amount")


class StakeholderDetail(BaseModel):
    """Detailed view of a deal stakeholder.

    Score Fields:
    - champion_potential: 0-100 scale (percentage, e.g., 85 means 85%)
    - engagement_score: 0-100 scale (percentage, e.g., 70 means 70%)

    Source Information:
    - source: Where the stakeholder was discovered (CRM, CALENDAR_DISCOVERY, EMAIL_DISCOVERY, MANUAL)
    - approval_status: Whether the stakeholder is approved for deal matching
        - CRM stakeholders are always APPROVED
        - Discovery stakeholders (first 5 per source) are auto-APPROVED
        - Additional discovery stakeholders are PENDING until approved
    """

    stakeholder_id: UUID
    deal_id: UUID
    contact_id: UUID | None = None
    email: str
    name: str | None = None
    job_title: str | None = None
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance
    decision_power: DecisionPower
    champion_potential: float = Field(
        ..., description="Champion potential score (0-100 scale, e.g., 85 = 85%)"
    )
    motivation: str | None = None
    pain_points: list[str] | None = None
    keywords: list[str] | None = None
    last_contact_at: datetime | None = None
    engagement_score: float = Field(
        ..., description="Engagement score (0-100 scale, e.g., 70 = 70%)"
    )
    temperature: StakeholderTemperature
    source: StakeholderSource = Field(
        ...,
        description="Where the stakeholder was discovered (CRM, CALENDAR_DISCOVERY, EMAIL_DISCOVERY, MANUAL)",
    )
    approval_status: ApprovalStatus = Field(
        ...,
        description="Approval status (APPROVED, PENDING, REJECTED). Only APPROVED stakeholders are used for deal matching.",
    )

    model_config = {"from_attributes": True}


class StakeholderEngagementSnapshot(BaseModel):
    """A point-in-time snapshot of stakeholder engagement metrics.

    Used to track how a stakeholder's engagement has changed over time.
    """

    recorded_at: datetime = Field(..., description="When this snapshot was recorded")
    stance: str | None = Field(None, description="Stakeholder stance at this point")
    temperature: str | None = Field(None, description="Engagement temperature at this point")
    engagement_score: float | None = Field(None, description="Engagement score (0-100)")
    champion_potential: float | None = Field(None, description="Champion potential (0-100)")
    decision_power: str | None = Field(None, description="Decision power level at this point")


class StakeholderFullDetail(BaseModel):
    """Full stakeholder detail including engagement history.

    This is returned when fetching a single stakeholder and includes
    the engagement history showing how the stakeholder's metrics have
    changed over time (e.g., from COLD to WARM to HOT).

    Score Fields:
    - champion_potential: 0-100 scale (percentage, e.g., 85 means 85%)
    - engagement_score: 0-100 scale (percentage, e.g., 70 means 70%)

    History:
    - engagement_history: Up to 15 most recent snapshots showing metric changes
    """

    stakeholder_id: UUID
    deal_id: UUID
    contact_id: UUID | None = None
    email: str
    name: str | None = None
    job_title: str | None = None
    department: str | None = Field(
        None, description="Inferred department from title or external sources"
    )
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance
    decision_power: DecisionPower
    champion_potential: float = Field(
        ..., description="Champion potential score (0-100 scale, e.g., 85 = 85%)"
    )
    motivation: str | None = None
    pain_points: list[str] | None = None
    keywords: list[str] | None = None
    last_contact_at: datetime | None = None
    engagement_score: float = Field(
        ..., description="Engagement score (0-100 scale, e.g., 70 = 70%)"
    )
    temperature: StakeholderTemperature
    source: StakeholderSource = Field(
        ...,
        description="Where the stakeholder was discovered (CRM, CALENDAR_DISCOVERY, EMAIL_DISCOVERY, MANUAL)",
    )
    approval_status: ApprovalStatus = Field(
        ...,
        description="Approval status (APPROVED, PENDING, REJECTED). Only APPROVED stakeholders are used for deal matching.",
    )
    engagement_history: list[StakeholderEngagementSnapshot] = Field(
        default_factory=list,
        description="Historical engagement snapshots showing how metrics changed over time (newest first, max 15 entries)",
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class StakeholderLink(BaseModel):
    platform: str = Field(..., description="Platform name (e.g., linkedin, twitter, crm)")
    url: str = Field(..., description="The URL")
    label: str | None = Field(None, description="Optional display label")
    added_at: datetime | None = Field(default_factory=datetime.utcnow)


class CreateStakeholderRequest(BaseModel):
    """Request model for manually creating a new stakeholder.

    Field Categories for Frontend UI:

    REQUIRED (always show):
    - email: Must be unique per deal

    PRIMARY (show prominently):
    - name, job_title, stance, decision_power
    - These are the most commonly filled fields

    SECONDARY (collapsible/advanced section):
    - department, champion_potential, motivation, pain_points, external_links
    - Power users may want to fill these upfront

    AUTO-SET (not in request):
    - source: Set to MANUAL
    - approval_status: Set to APPROVED
    - temperature: Set to UNKNOWN (calculated later)
    - engagement_score: Set to 0 (calculated later)
    """

    # REQUIRED
    email: str = Field(
        ...,
        description="Email address (required, must be unique per deal)",
        json_schema_extra={"ui_category": "required"},
    )

    # PRIMARY FIELDS (show prominently in UI)
    name: str | None = Field(
        None,
        description="Full name of the stakeholder (strongly recommended)",
        json_schema_extra={"ui_category": "primary", "ui_hint": "recommended"},
    )
    job_title: str | None = Field(
        None,
        description="Job title (e.g., 'VP of Engineering', 'Director of IT')",
        json_schema_extra={"ui_category": "primary"},
    )
    stance: StakeholderStance = Field(
        StakeholderStance.UNKNOWN,
        description="Stakeholder's attitude toward your solution. UNKNOWN if not yet assessed.",
        json_schema_extra={
            "ui_category": "primary",
            "ui_component": "select",
            "options": [
                {"value": "UNKNOWN", "label": "Unknown", "description": "Not yet determined"},
                {
                    "value": "CHAMPION",
                    "label": "Champion",
                    "description": "Actively advocates for your solution",
                },
                {"value": "SUPPORTER", "label": "Supporter", "description": "Generally positive"},
                {"value": "NEUTRAL", "label": "Neutral", "description": "No strong opinion"},
                {
                    "value": "DETRACTOR",
                    "label": "Detractor",
                    "description": "Has concerns or objections",
                },
                {"value": "BLOCKER", "label": "Blocker", "description": "Actively opposing"},
            ],
        },
    )
    decision_power: DecisionPower = Field(
        DecisionPower.UNKNOWN,
        description="Stakeholder's role in the buying decision",
        json_schema_extra={
            "ui_category": "primary",
            "ui_component": "select",
            "options": [
                {"value": "UNKNOWN", "label": "Unknown", "description": "Not yet determined"},
                {
                    "value": "DECISION_MAKER",
                    "label": "Decision Maker",
                    "description": "Has final authority and budget sign-off",
                },
                {
                    "value": "INFLUENCER",
                    "label": "Influencer",
                    "description": "Can sway decision makers",
                },
                {
                    "value": "EVALUATOR",
                    "label": "Evaluator",
                    "description": "Assesses technical/functional fit",
                },
                {
                    "value": "GATEKEEPER",
                    "label": "Gatekeeper",
                    "description": "Controls access to decision makers",
                },
                {
                    "value": "END_USER",
                    "label": "End User",
                    "description": "Will use the product daily",
                },
            ],
        },
    )

    # SECONDARY FIELDS (collapsible in UI)
    department: str | None = Field(
        None,
        description="Department (e.g., 'Engineering', 'Finance', 'IT')",
        json_schema_extra={"ui_category": "secondary"},
    )
    champion_potential: float | None = Field(
        None,
        ge=0,
        le=100,
        description="Your assessment of champion potential (0-100). Leave empty if unknown - defaults to 50 (neutral).",
        json_schema_extra={
            "ui_category": "secondary",
            "ui_component": "slider",
            "default_value": 50,
            "presets": [
                {"value": 0, "label": "Very Low"},
                {"value": 25, "label": "Low"},
                {"value": 50, "label": "Moderate (default)"},
                {"value": 75, "label": "High"},
                {"value": 100, "label": "Very High"},
            ],
        },
    )
    motivation: str | None = Field(
        None,
        description="What drives this stakeholder? What do they care about?",
        json_schema_extra={"ui_category": "secondary", "ui_component": "textarea"},
    )
    pain_points: list[str] | None = Field(
        None,
        description="Challenges or problems they've mentioned",
        json_schema_extra={"ui_category": "secondary", "ui_component": "tags"},
    )
    external_links: list[StakeholderLink] | None = Field(
        None,
        description="External profile links (LinkedIn, Twitter, etc.)",
        json_schema_extra={"ui_category": "secondary"},
    )


class AvailableContactItem(BaseModel):
    """Contact available for adding as a stakeholder.

    Only contacts with valid email addresses are returned.
    Includes professional context useful for stakeholder selection.
    """

    contact_id: UUID = Field(..., description="Internal contact identifier")
    external_id: str = Field(..., description="CRM provider's contact identifier")
    email: str = Field(..., description="Primary email address (required for stakeholders)")
    first_name: str | None = Field(None, description="Contact's first name")
    last_name: str | None = Field(None, description="Contact's last name")
    full_name: str | None = Field(None, description="Contact's display name")
    job_title: str | None = Field(None, description="Current job title")
    company_name: str | None = Field(None, description="Associated company name")
    phone: str | None = Field(None, description="Primary phone number")
    lifecycle_stage: str | None = Field(None, description="CRM lifecycle stage")
    last_activity_at: datetime | None = Field(None, description="Last activity timestamp")

    model_config = {"from_attributes": True}


class AddContactsAsStakeholdersRequest(BaseModel):
    """Request to add selected contacts as stakeholders."""

    contact_ids: list[UUID] = Field(
        ...,
        description="List of contact IDs to add as stakeholders",
        min_length=1,
    )


class SkippedContact(BaseModel):
    """Details about a contact that was skipped during stakeholder creation."""

    contact_id: UUID = Field(..., description="ID of the skipped contact")
    reason: str = Field(..., description="Reason why the contact was skipped")


class AddContactsAsStakeholdersResponse(BaseModel):
    """Response for adding contacts as stakeholders."""

    added: list[StakeholderDetail] = Field(
        default_factory=list,
        description="Successfully added stakeholders",
    )
    skipped: list[SkippedContact] = Field(
        default_factory=list,
        description="Contacts that were skipped (no email or already exists)",
    )
    total_requested: int = Field(..., description="Total number of contacts requested")
    total_added: int = Field(..., description="Number of contacts successfully added")
    total_skipped: int = Field(..., description="Number of contacts skipped")


class StakeholderListResponse(BaseModel):
    """Response for listing stakeholders with deal context.

    Includes company_domain for frontend domain validation when adding new stakeholders.
    """

    company_domain: str | None = Field(
        None,
        description="Company domain for the deal (e.g., 'acmecorp.com'). "
        "Frontend should validate that new stakeholder emails match this domain.",
    )
    company_name: str | None = Field(
        None,
        description="Company name for display purposes.",
    )
    stakeholders: list[StakeholderDetail] = Field(
        default_factory=list,
        description="List of stakeholders for the deal.",
    )


class DealJobResponse(BaseModel):
    """Response for deal job initiation (generic for all job types).

    Used when:
    - POST to initiate a new job (returns job_id to subscribe SSE)
    - GET to check for active jobs (for reconnection/recovery)
    """

    job_id: UUID | None = Field(
        None, description="Job ID for SSE subscription (None if no active job)"
    )
    job_type: str = Field(..., description="Type of job")
    status: str = Field(..., description="Current status of the job")


class MeetingJobsStatus(BaseModel):
    """Active jobs for a meeting, keyed by job type.

    Each field is None if no active job exists for that type.
    """

    meeting_briefing: DealJobResponse | None = None
    meeting_summary: DealJobResponse | None = None
    meeting_followup: DealJobResponse | None = None


class DealSummaryJobsStatus(BaseModel):
    """Active deal-level jobs (not meeting-level).

    Each field is None if no active job exists for that type.
    """

    deal_summary: DealJobResponse | None = None
    # Future: stakeholder_analysis, deal_risk_analysis, etc.


# Meeting Models


class MeetingAttendee(BaseModel):
    """Single attendee in a calendar event."""

    name: str | None
    email: str
    status: str | None


class DealMeetingResponse(BaseModel):
    """Meeting associated with a deal."""

    event_id: UUID
    subject: str | None
    start_at: datetime
    status: str  # AssociationStatus value
    attendees: list[MeetingAttendee]

    # Meeting Prep (Briefing) - Pre-meeting
    meeting_prep_status: str  # GenerationStatus value
    meeting_prep_data: dict[str, Any] | None = None

    # Meeting Summary - Post-meeting
    meeting_summary_status: str  # GenerationStatus value
    meeting_summary_data: dict[str, Any] | None = None

    # Meeting Follow-up - Post-meeting actions
    meeting_followup_status: str  # GenerationStatus value
    meeting_followup_data: dict[str, Any] | None = None

    # Active jobs (unified for all job types)
    active_jobs: MeetingJobsStatus | None = None

    model_config = {"from_attributes": True}


class UpdateMeetingStatusRequest(BaseModel):
    """Request model for updating meeting association status."""

    status: str  # AssociationStatus value


class ContactListItem(BaseModel):
    """Simplified contact model for selection lists."""

    contact_id: UUID
    external_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    job_title: str | None = None
    company_name: str | None = None

    model_config = {"from_attributes": True}
