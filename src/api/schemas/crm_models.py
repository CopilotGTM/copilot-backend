"""CRM API response models for deals and related entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)


class DealTeamMember(BaseModel):
    """A team member associated with a deal (owner, collaborator, split_owner, etc.)."""

    participant_id: UUID | None = Field(None, description="Deal participant row ID")
    user_id: UUID | None = Field(None, description="Internal user ID when linked")
    name: str | None = Field(None, description="Display name of the team member")
    email: str | None = Field(None, description="Email address of the team member")
    role: str = Field(description="Role in the deal: owner, collaborator, split_owner, etc.")

    model_config = {"from_attributes": True}


class DealOwnerDetail(BaseModel):
    """Owner details for a deal.

    This is exposed as a list in list responses for forward compatibility
    (e.g., multiple owners in some CRMs).
    """

    owner_id: UUID | None = Field(None, description="Internal owner ID for filtering and linking")
    name: str | None = Field(None, description="Display name of the owner")
    email: str | None = Field(None, description="Email address of the owner")

    model_config = {"from_attributes": True}


class DealOwnerFilterItem(BaseModel):
    """Owner option for deal filter dropdowns."""

    owner_id: UUID = Field(description="Internal owner ID for filtering")
    name: str | None = Field(None, description="Display name of the owner")
    email: str | None = Field(None, description="Email address of the owner")

    model_config = {"from_attributes": True}


class DealOwnerFilters(BaseModel):
    """List of distinct owners available for deal filtering."""

    items: list[DealOwnerFilterItem]


class DealListItem(BaseModel):
    """Individual deal item in a list response."""

    deal_id: UUID
    name: str | None
    amount: float | None
    predicted_amount: float | None = None
    currency: str | None
    currency_symbol: str | None = Field(
        None, description="Display symbol derived from currency code (for example $, ₹)."
    )
    deal_status: str
    probability: float | None
    close_date: datetime | None
    expected_close_date: datetime | None = None
    stage_id: UUID | None = None
    stage_name: str | None
    stage_label: str | None
    pipeline_id: UUID | None = None
    pipeline_name: str | None
    pipeline_label: str | None
    owner_details: list[DealOwnerDetail] = Field(
        default_factory=list,
        description="Owner details as a list (typically 0..1 items).",
    )
    company_id: UUID | None = None
    company_name: str | None = None
    company_domain: str | None = None
    primary_contact_id: UUID | None = None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None
    last_activity_at: datetime | None = None
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


class DealImportJobStatus(BaseModel):
    """Reserved import job metadata for a future rebuilt import flow."""

    job_id: UUID = Field(..., description="Deal import job ID")
    job_type: str = Field(..., description="Job type (always `deal_import` for this payload)")
    status: str = Field(..., description="Current job status")
    stream_url: str = Field(..., description="SSE URL to reconnect to the active import job")


class DealsPage(BaseModel):
    """Paginated list of deals."""

    items: list[DealListItem]
    active_import_job: DealImportJobStatus | None = Field(
        None,
        description=(
            "Reserved for the future deal import rebuild. Always null while CSV import is disabled."
        ),
    )
    page: int = Field(..., description="Current page number (1-indexed)")
    limit: int = Field(..., description="Maximum number of items per page")
    total_count: int = Field(..., description="Total number of deals matching filters")
    total_pages: int = Field(..., description="Total number of pages")


class DealSearchItem(BaseModel):
    """Lightweight deal item returned by cursor search."""

    deal_id: UUID
    name: str | None
    amount: float | None
    predicted_amount: float | None = None
    currency: str | None
    currency_symbol: str | None = None
    deal_status: str | None
    stage_id: UUID | None = None
    stage_name: str | None
    pipeline_id: UUID | None = None
    pipeline_name: str | None
    company_id: UUID | None = None
    company_name: str | None = None
    company_domain: str | None = None
    owner_details: list[DealOwnerDetail] = Field(default_factory=list)
    is_internal: bool = Field(
        ...,
        description="True if this deal is from internal CRM and can be edited.",
    )
    created_at: datetime
    updated_at: datetime


class DealSearchPage(BaseModel):
    """Cursor page for lightweight deal search."""

    items: list[DealSearchItem]
    next_cursor: str | None = None
    has_more: bool = False


class DealCustomColumnDefinition(BaseModel):
    """Custom column metadata for frontend control rendering.

    Returned inline with deal detail to avoid an additional fetch for custom
    field schema before rendering edit controls. This keeps frontend rendering
    deterministic even when custom values are null.
    """

    key: str = Field(..., description="Stable custom field key")
    label: str = Field(..., description="Display label for the field")
    data_type: str = Field(
        ...,
        description=(
            "Custom field type used to choose UI controls "
            "(text/number/integer/boolean/date/datetime/currency/etc.)"
        ),
    )
    required: bool = Field(False, description="Whether value is required")
    options: dict[str, Any] = Field(default_factory=dict, description="Type-specific UI options")
    validation: dict[str, Any] = Field(
        default_factory=dict, description="Validation metadata for frontend hints"
    )
    currency_code: str | None = Field(
        None,
        description=(
            "Currency code context for currency custom fields. "
            "Defaults to the deal currency when field-level override is absent."
        ),
    )
    currency_symbol: str | None = Field(
        None, description="Currency symbol resolved from currency_code (for example $, ₹)."
    )
    editable: bool = Field(
        False,
        description=(
            "True when this custom field can be edited through custom_columns "
            "on internal CRM deals."
        ),
    )
    patch_field: str | None = Field(
        None,
        description="PATCH path for custom field edits, for example custom_columns.custom_key.",
    )
    edit_control: str | None = Field(None, description="Frontend edit control hint")
    edit_policy: str = Field(
        "read_only",
        description="custom_columns for editable custom fields, formula/read_only otherwise.",
    )
    locked_reason: str | None = Field(None, description="Reason shown when editing is locked")


class DealDetailFieldDefinition(BaseModel):
    """Frontend metadata for rendering one predefined deal detail field."""

    key: str = Field(..., description="Dotted or top-level path in DealDetail")
    label: str = Field(..., description="Display label")
    data_type: str = Field(..., description="Frontend data type")
    order: int = Field(..., description="Sort order for Get Details rendering")
    section: str = Field(..., description="Logical detail section")
    filter_control: str = Field(..., description="Frontend filter/control hint")
    editable: bool = Field(
        False,
        description=(
            "True when PATCH /deals/{deal_id} accepts it for internal CRM deals. "
            "External CRM deals remain read-only through is_internal=false."
        ),
    )
    patch_field: str | None = Field(None, description="PATCH field for inline edits")
    edit_control: str | None = Field(None, description="Inline edit control hint")
    edit_policy: str = Field("read_only", description="Why or how this field can be edited")
    locked_reason: str | None = Field(None, description="Reason shown when editing is locked")


_DEAL_DETAIL_FIELD_ROWS: tuple[tuple[str, str, str, int, str, str], ...] = (
    ("name", "Deal name", "string", 1, "core", "text"),
    ("deal_status", "Status", "enum", 2, "core", "select"),
    ("stage_name", "Stage", "string", 3, "core", "select"),
    ("pipeline_name", "Pipeline", "string", 4, "core", "select"),
    ("amount", "Amount", "number", 5, "value", "currency"),
    ("currency", "Currency", "string", 6, "value", "select"),
    ("predicted_amount", "Predicted amount", "number", 7, "value", "currency"),
    ("probability", "Probability", "number", 8, "value", "percent"),
    ("forecast_category", "Forecast category", "string", 9, "value", "select"),
    ("owner_details", "Owner", "array", 12, "ownership", "people"),
    ("deal_team", "Internal team", "array", 13, "ownership", "people"),
    ("company_name", "Account", "string", 14, "account", "account"),
    ("company_domain", "Account domain", "string", 15, "account", "domain"),
    ("company_industry", "Industry", "string", 16, "account", "select"),
    ("company_website", "Website", "url", 17, "account", "link"),
    ("company_linkedin_url", "Company LinkedIn", "url", 18, "account", "link"),
    ("company_country", "Account country", "string", 19, "account", "select"),
    ("primary_contact_name", "Primary contact", "string", 20, "contact", "contact"),
    ("primary_contact_email", "Primary contact email", "email", 21, "contact", "email"),
    ("source_created_at", "CRM created date", "datetime", 22, "timeline", "date_range"),
    ("close_date", "Close date", "datetime", 23, "timeline", "date_range"),
    ("expected_close_date", "Expected close date", "datetime", 24, "timeline", "date_range"),
    ("closed_won_at", "Won date", "datetime", 25, "timeline", "date_range"),
    ("closed_lost_at", "Lost date", "datetime", 26, "timeline", "date_range"),
    ("deal_age_days", "Deal age", "integer", 27, "timeline", "number"),
    ("last_activity_at", "Last activity", "datetime", 28, "timeline", "date_range"),
    ("stage_entered_at", "Stage entered", "datetime", 29, "timeline", "date_range"),
    ("stage_updated_at", "Stage updated", "datetime", 30, "timeline", "date_range"),
    ("deal_type", "Deal type", "string", 31, "process", "select"),
    ("priority", "Priority", "string", 32, "process", "select"),
    ("lead_source", "Lead source", "string", 33, "process", "select"),
    ("closed_reason", "Closed reason", "string", 34, "process", "long_text"),
    ("crm_url", "CRM link", "url", 35, "process", "link"),
    ("description", "Description", "string", 36, "process", "long_text"),
)

_INLINE_EDITABLE_DEAL_FIELDS = {
    "name",
    "amount",
    "currency",
    "probability",
    "forecast_category",
    "source_created_at",
    "expected_close_date",
    "deal_type",
    "priority",
    "lead_source",
    "description",
}
_DEAL_EDIT_CONTROLS = {
    "name": "text",
    "amount": "currency",
    "currency": "select",
    "probability": "percent",
    "forecast_category": "select",
    "source_created_at": "datetime",
    "expected_close_date": "datetime",
    "deal_type": "select",
    "priority": "select",
    "lead_source": "select",
    "description": "textarea",
}
_DEAL_STAGE_FIELDS = {"deal_status", "stage_name", "pipeline_name"}
_DEAL_ACCOUNT_FIELDS = {
    "company_name",
    "company_domain",
    "company_industry",
    "company_website",
    "company_linkedin_url",
    "company_country",
}
_DEAL_PRIMARY_CONTACT_FIELDS = {"primary_contact_name", "primary_contact_email"}
_DEAL_LIFECYCLE_FIELDS = {"close_date", "closed_won_at", "closed_lost_at", "closed_reason"}
_DEAL_SYSTEM_FIELDS = {
    "predicted_amount",
    "deal_age_days",
    "last_activity_at",
    "stage_entered_at",
    "stage_updated_at",
    "crm_url",
}


def _deal_edit_metadata(key: str) -> tuple[bool, str | None, str | None, str, str | None]:
    if key in _INLINE_EDITABLE_DEAL_FIELDS:
        return True, key, _DEAL_EDIT_CONTROLS.get(key), "always", None
    if key in _DEAL_STAGE_FIELDS:
        return False, None, None, "stage_action", "Use the deal stage/lifecycle action."
    if key == "owner_details":
        return False, None, None, "owner_assignment", "Use the deal owner assignment action."
    if key == "deal_team":
        return False, None, None, "participants_action", "Use deal participant actions."
    if key in _DEAL_ACCOUNT_FIELDS:
        return False, None, None, "account_action", "Use the deal account update action."
    if key in _DEAL_PRIMARY_CONTACT_FIELDS:
        return (
            False,
            None,
            None,
            "primary_contact_action",
            "Use the deal primary-contact update action.",
        )
    if key in _DEAL_LIFECYCLE_FIELDS:
        return False, None, None, "lifecycle_action", "Use close or reopen actions."
    if key in _DEAL_SYSTEM_FIELDS:
        return False, None, None, "system", "System-managed value."
    return False, None, None, "read_only", "Read-only deal detail."


def deal_detail_field_definitions() -> list[DealDetailFieldDefinition]:
    """Return ordered predefined deal detail metadata for the Get Details panel."""
    definitions: list[DealDetailFieldDefinition] = []
    for key, label, data_type, order, section, filter_control in _DEAL_DETAIL_FIELD_ROWS:
        editable, patch_field, edit_control, edit_policy, locked_reason = _deal_edit_metadata(key)
        definitions.append(
            DealDetailFieldDefinition(
                key=key,
                label=label,
                data_type=data_type,
                order=order,
                section=section,
                filter_control=filter_control,
                editable=editable,
                patch_field=patch_field,
                edit_control=edit_control,
                edit_policy=edit_policy,
                locked_reason=locked_reason,
            )
        )
    return definitions


class DealDetail(BaseModel):
    """Detailed deal information - streamlined for frontend needs."""

    # Identity
    deal_id: UUID
    name: str | None

    # Deal value
    amount: float | None
    currency: str | None
    currency_symbol: str | None = Field(
        None, description="Display symbol derived from currency code (for example $, ₹)."
    )
    predicted_amount: float | None = None

    # Status & Stage
    deal_status: str
    probability: float | None = None
    forecast_category: str | None = None
    stage_id: UUID | None = None
    stage_name: str | None
    stage_label: str | None
    pipeline_id: UUID | None = None
    pipeline_name: str | None = None
    pipeline_label: str | None = None

    # Timeline (frontend: Creation Date, Close Date, Deal Age, Last Communication)
    source_created_at: datetime | None
    close_date: datetime | None
    expected_close_date: datetime | None = None
    closed_won_at: datetime | None = None
    closed_lost_at: datetime | None = None
    last_activity_at: datetime | None
    stage_entered_at: datetime | None = None
    stage_updated_at: datetime | None = None
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
    company_id: UUID | None = None
    company_name: str | None = None
    company_domain: str | None = None
    company_industry: str | None = Field(None, description="Industry of the associated company")
    company_website: str | None = Field(None, description="Website of the associated company")
    company_linkedin_url: str | None = Field(
        None, description="LinkedIn URL of the associated company"
    )
    company_country: str | None = Field(None, description="Country of the associated company")
    primary_contact_id: UUID | None = None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None

    # Additional details
    deal_type: str | None = None
    priority: str | None = None
    lead_source: str | None = None
    closed_reason: str | None = None
    crm_url: str | None = None
    description: str | None = None

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

    # Internal CRM import custom columns (custom_* from CSV imports).
    custom_columns: dict[str, Any] | None = Field(
        None,
        description="Imported custom column key/value pairs for internal CRM deals. Null when absent or for external CRM deals.",
    )
    custom_column_definitions: list[DealCustomColumnDefinition] | None = Field(
        None,
        description=(
            "Active custom field definitions for internal CRM deals. "
            "Frontend should use data_type to render date/datetime/currency controls."
        ),
    )
    detail_field_definitions: list[DealDetailFieldDefinition] = Field(
        default_factory=deal_detail_field_definitions,
        description=(
            "Ordered metadata for predefined deal detail fields. "
            "Custom fields continue to use custom_column_definitions."
        ),
    )

    # Time tracked (placeholder for future implementation)
    time_tracked: str | None = Field(None, description="Total time tracked on this deal")

    # System/read-only metadata
    source_modified_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    synced_at: datetime
    associated_contact_count: int | None = None
    associated_company_count: int | None = None
    note_count: int | None = None

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

    Source Information:
    - source: Where the stakeholder was discovered (CRM, CALENDAR_DISCOVERY, EMAIL_DISCOVERY, MANUAL)
    - approval_status: Whether the stakeholder is approved for deal matching
        - CRM stakeholders are always APPROVED
        - Discovery stakeholders (first 5 per source) are auto-APPROVED
        - Additional discovery stakeholders are PENDING until approved
    """

    participant_id: UUID = Field(..., description="Canonical CRMDealParticipant ID")
    deal_id: UUID
    contact_id: UUID | None = None
    email: str | None = None
    name: str | None = None
    job_title: str | None = None
    phone_number: str | None = Field(None, description="Phone number as provided by CRM or user")
    linkedin_url: str | None = Field(None, description="LinkedIn profile URL")
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance
    decision_power: DecisionPower
    motivation: str | None = None
    pain_points: list[str] | None = None
    keywords: list[str] | None = None
    last_contact_at: datetime | None = None
    temperature: StakeholderTemperature
    source: StakeholderSource = Field(
        ...,
        description="Where the stakeholder was discovered (CRM, CALENDAR_DISCOVERY, EMAIL_DISCOVERY, MANUAL)",
    )
    approval_status: ApprovalStatus = Field(
        ...,
        description="Approval status (APPROVED, PENDING, REJECTED). Only APPROVED stakeholders are used for deal matching.",
    )
    active_research_job: DealJobResponse | None = Field(
        None,
        description="Active manual stakeholder research job for reconnect, if any.",
    )
    stakeholder_research: str | None = Field(
        None,
        description="Cached stakeholder/contact research profile, when already available.",
    )
    last_researched_at: datetime | None = Field(
        None,
        description="When stakeholder/contact research was last generated.",
    )

    model_config = {"from_attributes": True}


class StakeholderEngagementSnapshot(BaseModel):
    """A point-in-time snapshot of stakeholder engagement metrics.

    Used to track how a stakeholder's engagement has changed over time.
    """

    recorded_at: datetime = Field(..., description="When this snapshot was recorded")
    stance: str | None = Field(None, description="Stakeholder stance at this point")
    temperature: str | None = Field(None, description="Engagement temperature at this point")
    decision_power: str | None = Field(None, description="Decision power level at this point")


class StakeholderFullDetail(BaseModel):
    """Full stakeholder detail including engagement history.

    This is returned when fetching a single stakeholder and includes
    the engagement history showing how the stakeholder's metrics have
    changed over time (e.g., from COLD to WARM to HOT).

    History:
    - engagement_history: Up to 15 most recent snapshots showing metric changes
    """

    participant_id: UUID = Field(..., description="Canonical CRMDealParticipant ID")
    deal_id: UUID
    contact_id: UUID | None = None
    email: str | None = None
    name: str | None = None
    job_title: str | None = None
    phone_number: str | None = Field(None, description="Phone number as provided by CRM or user")
    linkedin_url: str | None = Field(None, description="LinkedIn profile URL")
    department: str | None = Field(
        None, description="Inferred department from title or external sources"
    )
    external_links: list[StakeholderLink] | None = None
    stance: StakeholderStance
    decision_power: DecisionPower
    motivation: str | None = None
    pain_points: list[str] | None = None
    keywords: list[str] | None = None
    last_contact_at: datetime | None = None
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
    stakeholder_research: str | None = Field(
        None,
        description="Markdown research profile generated by StakeholderResearchAgent",
    )
    last_researched_at: datetime | None = Field(
        None,
        description="When the stakeholder research was last generated",
    )
    active_research_job: DealJobResponse | None = Field(
        None,
        description="Active manual stakeholder research job for reconnect, if any.",
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class StakeholderLink(BaseModel):
    """External profile link attached to a stakeholder for UI display.

    The schema carries both raw link data and optional display labels so clients
    can render compact contact cards without looking up platform metadata.
    """

    platform: str = Field(..., description="Platform name (e.g., linkedin, twitter, crm)")
    url: str = Field(..., description="The URL")
    label: str | None = Field(None, description="Optional display label")
    added_at: datetime | None = Field(default_factory=datetime.utcnow)


class CreateStakeholderRequest(BaseModel):
    """Request model for manually creating a new stakeholder.

    Frontend create forms should send only contact_id, name, email, and/or
    linkedin_url. At least one explicit create field must be non-empty.

    CREATE IDENTITY FIELDS:
    - name
    - email
    - linkedin_url
    - contact_id (when selected from contact search)

    LEGACY ACCEPTED FIELDS:
    - job_title, phone_number, stance, decision_power, department,
      motivation, pain_points
    - Accepted during rollout/backward compatibility; do not render these in
      the add-stakeholder frontend form.

    AUTO-FILLED (not frontend-owned on create):
    - source: Set to MANUAL
    - approval_status: Set to APPROVED
    - temperature: Set to UNKNOWN (calculated later)
    - profile, buying-role, and engagement fields are resolved by contact
      resolution and enrichment after creation.
    """

    contact_id: UUID | None = Field(
        None,
        description=(
            "Existing CRM contact selected from contact search. When provided, "
            "the backend fills stakeholder profile fields from that contact."
        ),
        json_schema_extra={"ui_category": "primary"},
    )
    email: str | None = Field(
        None,
        description="Email address, when known",
        json_schema_extra={"ui_category": "primary"},
    )

    # Identity fields for the create form
    name: str | None = Field(
        None,
        description="Full name of the stakeholder (strongly recommended)",
        json_schema_extra={"ui_category": "primary", "ui_hint": "recommended"},
    )
    job_title: str | None = Field(
        None,
        description="Legacy create field. Auto-filled after creation; use PATCH to edit later.",
        json_schema_extra={"ui_category": "legacy"},
    )
    phone_number: str | None = Field(
        None,
        description="Legacy create field. Auto-filled after creation; use PATCH to edit later.",
        json_schema_extra={"ui_category": "legacy"},
    )
    linkedin_url: str | None = Field(
        None,
        description="LinkedIn profile URL",
        json_schema_extra={"ui_category": "primary"},
    )
    stance: StakeholderStance = Field(
        StakeholderStance.UNKNOWN,
        description="Legacy create field. Auto-filled after creation; use PATCH to edit later.",
        json_schema_extra={
            "ui_category": "legacy",
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
        description="Legacy create field. Auto-filled after creation; use PATCH to edit later.",
        json_schema_extra={
            "ui_category": "legacy",
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

    # Legacy create fields accepted for backward compatibility
    department: str | None = Field(
        None,
        description="Legacy create field. Auto-filled after creation; use PATCH to edit later.",
        json_schema_extra={"ui_category": "legacy"},
    )
    motivation: str | None = Field(
        None,
        description="Legacy create field. Auto-filled after creation; use PATCH to edit later.",
        json_schema_extra={"ui_category": "legacy", "ui_component": "textarea"},
    )
    pain_points: list[str] | None = Field(
        None,
        description="Legacy create field. Auto-filled after creation; use PATCH to edit later.",
        json_schema_extra={"ui_category": "legacy", "ui_component": "tags"},
    )

    @model_validator(mode="after")
    def require_identity(self) -> CreateStakeholderRequest:
        """Require one explicit person key unless a CRM contact was selected."""
        if self.contact_id:
            return self
        if any(value and value.strip() for value in (self.email, self.linkedin_url, self.name)):
            return self
        raise ValueError("Provide a stakeholder name, email, LinkedIn URL, or contact ID")


class UpdateStakeholderRequest(BaseModel):
    """Request model for partially updating an existing stakeholder."""

    name: str | None = Field(
        None,
        description="Full name of the stakeholder",
    )
    job_title: str | None = Field(
        None,
        description="Job title (e.g., 'VP of Engineering', 'Director of IT')",
    )
    phone_number: str | None = Field(
        None,
        description="Phone number (stored as-is, no formatting applied)",
    )
    linkedin_url: str | None = Field(
        None,
        description="LinkedIn profile URL",
    )
    department: str | None = Field(
        None,
        description="Department (e.g., 'Engineering', 'Finance', 'IT')",
    )
    stance: StakeholderStance | None = Field(
        None,
        description="Stakeholder's current stance toward your solution",
    )
    decision_power: DecisionPower | None = Field(
        None,
        description="Stakeholder's role in the buying decision",
    )
    motivation: str | None = Field(
        None,
        description="What drives this stakeholder? What do they care about?",
    )
    pain_points: list[str] | None = Field(
        None,
        description="Challenges or problems they've mentioned",
    )
    external_links: list[StakeholderLink] | None = Field(
        None,
        description="External profile links (LinkedIn, Twitter, etc.)",
    )


class AvailableContactItem(BaseModel):
    """Legacy contact picker item for add-stakeholder compatibility."""

    contact_id: UUID = Field(..., description="Internal contact identifier")
    email: str = Field(..., description="Primary email address")
    first_name: str | None = Field(None, description="Contact's first name")
    last_name: str | None = Field(None, description="Contact's last name")
    full_name: str | None = Field(None, description="Contact's display name")
    job_title: str | None = Field(None, description="Current job title")
    company_name: str | None = Field(None, description="Associated company name")
    phone: str | None = Field(None, description="Primary phone number")
    lifecycle_stage: str | None = Field(None, description="CRM lifecycle stage")
    last_activity_at: datetime | None = Field(None, description="Last activity timestamp")


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


class CreateStakeholderResponse(BaseModel):
    """Response for manual stakeholder creation.

    The stakeholder row is created synchronously. Any post-create background work
    is returned as deal jobs so clients can show progress and reconnect.
    """

    stakeholder: StakeholderDetail
    processing_jobs: list[DealJobResponse] = Field(default_factory=list)
    research_job: DealJobResponse | None = Field(
        None,
        description="Research job for the added/restored stakeholder, if one was started.",
    )


class StakeholderConflictResponse(BaseModel):
    """Metadata returned when a create request targets an existing stakeholder."""

    participant_id: UUID
    approval_status: str
    is_deleted: bool


class MeetingJobsStatus(BaseModel):
    """Active jobs for a meeting, keyed by job type.

    Each field is None if no active job exists for that type.
    """

    meeting_briefing: DealJobResponse | None = None
    meeting_summary: DealJobResponse | None = None
    meeting_followup: DealJobResponse | None = None
    transcript_upload: DealJobResponse | None = None


class DealSummaryJobsStatus(BaseModel):
    """Active deal-level jobs (not meeting-level).

    Each field is None if no active job exists for that type.
    """

    deal_summary: DealJobResponse | None = None
    stakeholder_processing: list[DealJobResponse] = Field(default_factory=list)


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


class TranscriptPresignRequest(BaseModel):
    """Request a presigned URL to upload a transcript file."""

    filename: str = Field(..., min_length=1, max_length=255)
    mimetype: str = Field(..., max_length=255)
    size_bytes: int = Field(..., gt=0)


class TranscriptPresignResponse(BaseModel):
    """Presigned upload URL for a transcript file."""

    job_id: UUID
    upload_url: str = Field(description="SAS URL for direct upload (empty if job already exists)")
    expires_in_seconds: int = Field(description="Seconds until the upload URL expires")
    status: str


class TranscriptFinalizeRequest(BaseModel):
    """Confirm that a transcript file upload has completed."""

    job_id: UUID = Field(..., description="The job ID returned by the presign endpoint")


class ContactListItem(BaseModel):
    """Simplified contact model for selection lists."""

    contact_id: UUID
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    job_title: str | None = None
    company_name: str | None = None

    model_config = {"from_attributes": True}


class DealsListParams(NamedTuple):
    """Parsed query parameters for listing deals.

    Note: This is a NamedTuple (not Pydantic BaseModel) because FastAPI only properly
    parses list query parameters (like owner_ids) when Query() is in a function signature.
    Use the get_deals_list_params() dependency function to parse query params into this type.
    """

    limit: int
    page: int
    statuses: list[CRMDealStatus] | None
    pipeline_ids: list[UUID] | None
    owner_ids: list[UUID] | None
    min_amount: float | None
    max_amount: float | None
    min_close_date: datetime | None
    max_close_date: datetime | None
    min_expected_close_date: datetime | None
    max_expected_close_date: datetime | None
    q: str | None
