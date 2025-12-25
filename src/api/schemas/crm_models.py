"""CRM API response models for deals and related entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
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

    # Time tracked (placeholder for future implementation)
    time_tracked: str | None = Field(None, description="Total time tracked on this deal")

    # Deal Summary (AI-generated)
    deal_summary_status: str | None = Field(
        None,
        description="Computed status: NOT_STARTED (no data), IN_PROGRESS (active job), COMPLETED (data exists)",
    )
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
    """Detailed view of a deal stakeholder."""

    stakeholder_id: UUID
    deal_id: UUID
    contact_id: UUID | None = None
    email: str
    name: str | None = None
    job_title: str | None = None
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance
    decision_power: DecisionPower
    champion_potential: float
    motivation: str | None = None
    pain_points: list[str] | None = None
    keywords: list[str] | None = None
    last_contact_at: datetime | None = None
    engagement_score: float
    temperature: StakeholderTemperature
    source: StakeholderSource

    model_config = {"from_attributes": True}


class StakeholderLink(BaseModel):
    platform: str = Field(..., description="Platform name (e.g., linkedin, twitter, crm)")
    url: str = Field(..., description="The URL")
    label: str | None = Field(None, description="Optional display label")
    added_at: datetime | None = Field(default_factory=datetime.utcnow)


class CreateStakeholderRequest(BaseModel):
    """Request model for creating a new stakeholder."""

    email: str
    name: str | None = None
    job_title: str | None = None
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance = StakeholderStance.UNKNOWN
    decision_power: DecisionPower = DecisionPower.UNKNOWN


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


# ---------------------------------------------------------------------------
# Meeting Models
# ---------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# Deal Timeline Models
# -----------------------------------------------------------------------------


class EmailEventData(BaseModel):
    """Email-specific data for timeline events."""

    # Provider UI metadata (derived from TimelineEvent.event_source)
    provider_logo_url: str | None = None

    # --- List Preview (always present) ---
    subject: str | None = None
    snippet: str | None = None
    from_email: str | None = None

    # --- Detail Enrichment (populated on detail fetch) ---
    to_emails: list[str] | None = None
    cc_emails: list[str] | None = None
    thread_id: str | None = None
    has_attachments: bool | None = None


class MeetingSummary(BaseModel):
    """Meeting summary block with status and data."""

    status: str | None = Field(None, description="PENDING, PROCESSING, COMPLETED, FAILED")
    data: dict[str, Any] | None = None


class MeetingEventData(BaseModel):
    """Meeting-specific data for timeline events."""

    # Provider UI metadata (derived from TimelineEvent.event_source)
    provider_logo_url: str | None = None

    # --- List Preview (always present) ---
    title: str | None = None
    duration_minutes: int | None = None
    attendee_count: int | None = None

    # --- Detail Enrichment (populated on detail fetch) ---
    attendees: list[str] | None = None
    meeting_summary: MeetingSummary | None = None


class TimelineEvent(BaseModel):
    """Unified timeline event model for both list and detail views.

    List API returns preview data in event_data.
    Detail API returns same structure with enriched event_data.
    """

    id: UUID = Field(..., description="Timeline event ID")
    deal_id: UUID
    event_type: str = Field(..., description="EMAIL or MEETING")
    event_source: str = Field(..., description="EMAIL_GMAIL, CALENDAR_GOOGLE, etc.")
    event_timestamp: datetime
    event_data: dict[str, Any] = Field(
        ...,
        description="Type-specific data; shape varies by event_type (EmailEventData or MeetingEventData)",
    )
    excluded_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class TimelinePage(BaseModel):
    """Paginated list of timeline events."""

    deal_id: UUID
    items: list[TimelineEvent]
    total_count: int
    cursor: str | None = Field(None, description="Cursor for next page; null if no more results")
