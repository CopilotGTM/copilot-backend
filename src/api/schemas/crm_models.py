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


class PipelineStage(BaseModel):
    """Definition of a stage within a pipeline."""

    stage_id: UUID
    external_id: str
    name: str
    label: str | None = None
    display_order: int | None = None
    probability: float | None = None
    is_closed: bool = False

    # Merged history context
    status: str = "upcoming"  # upcoming, active, completed, skipped
    entered_at: datetime | None = None
    exited_at: datetime | None = None
    duration_days: int | None = None

    model_config = {"from_attributes": True}


class DealPipelineContext(BaseModel):
    """Snapshot of the pipeline and stage for a deal."""

    pipeline_id: UUID | None = None
    pipeline_external_id: str | None = None
    pipeline_name: str | None = None
    pipeline_label: str | None = None
    stage_id: UUID | None = None
    stage_external_id: str | None = None
    stage_name: str | None = None
    stage_label: str | None = None
    forecast_category: str | None = None
    deal_type: str | None = None
    priority: str | None = None
    lead_source: str | None = None
    stages: list[PipelineStage] | None = None

    model_config = {"from_attributes": True}


class DealTimelineContext(BaseModel):
    """Key timeline markers for a deal."""

    source_created_at: datetime | None = None
    close_date: datetime | None = None
    expected_close_date: datetime | None = None
    closed_won_at: datetime | None = None
    closed_lost_at: datetime | None = None
    last_activity_at: datetime | None = None
    stage_entered_at: datetime | None = None
    stage_updated_at: datetime | None = None
    synced_at: datetime | None = None
    source_modified_at: datetime | None = None
    last_seen_at: datetime | None = None
    days_to_close: int | None = None
    deal_age_days: int | None = None

    model_config = {"from_attributes": True}


class DealFinancialContext(BaseModel):
    """Financial snapshot for a deal."""

    amount: float | None = None
    currency: str | None = None
    amount_home_currency: float | None = None
    predicted_amount: float | None = None
    probability: float | None = None

    model_config = {"from_attributes": True}


class DealAssociationsContext(BaseModel):
    """External entities linked to a deal."""

    owner_external_id: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    technical_owner_external_id: str | None = None
    technical_owner_name: str | None = None
    technical_owner_email: str | None = None
    company_external_id: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    company_industry: str | None = None
    primary_contact_external_id: str | None = None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None

    model_config = {"from_attributes": True}


class DealStageHistoryItem(BaseModel):
    """Single stage transition in deal history."""

    history_id: UUID
    stage_external_id: str | None = None
    stage_name: str | None = None
    stage_label: str | None = None
    from_stage_external_id: str | None = None
    entered_at: datetime
    exited_at: datetime | None = None
    duration_days: int | None = None
    amount_at_stage: float | None = None
    probability_at_stage: float | None = None
    source: str | None = None

    model_config = {"from_attributes": True}


class DealContext(BaseModel):
    """Composite context block used by deal responses."""

    pipeline: DealPipelineContext | None = None
    timeline: DealTimelineContext | None = None
    financials: DealFinancialContext | None = None
    associations: DealAssociationsContext | None = None
    stage_history: list[DealStageHistoryItem] | None = None
    metrics: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None
    health: str | None = None
    time_tracked: str | None = None

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
    owner_name: str | None
    owner_email: str | None
    company_external_id: str | None
    company_name: str | None = None
    company_domain: str | None = None
    primary_contact_external_id: str | None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None
    created_at: datetime
    updated_at: datetime
    synced_at: datetime

    model_config = {"from_attributes": True}


class DealsPage(BaseModel):
    """Paginated list of deals."""

    items: list[DealListItem]
    cursor: str | None = Field(None, description="Cursor for next page; null if no more results")


class DealDetail(BaseModel):
    """Detailed deal information including all available fields."""

    # Identity
    deal_id: UUID
    external_id: str

    # Core business context
    name: str | None
    amount: float | None
    currency: str | None
    amount_home_currency: float | None
    deal_status: str
    probability: float | None
    predicted_amount: float | None
    close_date: datetime | None
    expected_close_date: datetime | None
    closed_won_at: datetime | None
    closed_lost_at: datetime | None
    closed_reason: str | None

    # Sales process snapshot
    stage_external_id: str | None
    stage_name: str | None
    stage_label: str | None
    pipeline_external_id: str | None
    pipeline_name: str | None
    pipeline_label: str | None
    pipeline_id: UUID | None
    stage_id: UUID | None
    forecast_category: str | None
    deal_type: str | None
    priority: str | None
    lead_source: str | None

    # Ownership & associations
    owner_external_id: str | None
    owner_name: str | None
    owner_email: str | None
    company_external_id: str | None
    company_name: str | None = None
    company_domain: str | None = None
    primary_contact_external_id: str | None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None

    # Activity & timing
    source_created_at: datetime | None
    last_activity_at: datetime | None
    stage_entered_at: datetime | None
    stage_updated_at: datetime | None
    synced_at: datetime
    source_modified_at: datetime | None
    last_seen_at: datetime | None

    # Freeform details
    description: str | None

    # Integration metadata
    integration_id: UUID
    organisation_id: UUID

    # Timestamps
    created_at: datetime
    updated_at: datetime
    context: DealContext | None = None

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
