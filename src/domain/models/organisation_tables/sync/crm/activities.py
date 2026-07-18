"""Canonical CRM activity models across providers.

This module provides a generalized activity model that can represent various
CRM activity types (tasks, calls, meetings, emails, notes) and link them to
any CRM object (deals, contacts, companies) via polymorphic relationships.

Replaces the deal-specific CRMDealTask model with a more flexible approach
that better supports multi-CRM integration patterns.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ...base_mixins import utcnow
from ..base import FlexibleSyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMActivityStatus(StrEnum):
    """Canonical activity status values across CRM providers."""

    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    WAITING = "WAITING"
    DEFERRED = "DEFERRED"
    CANCELLED = "CANCELLED"


class CRMActivityType(StrEnum):
    """Canonical activity type values across CRM providers.

    Covers common activity types found in major CRMs:
    - Salesforce: Task, Event, Call, Email
    - HubSpot: Task, Call, Meeting, Email, Note
    - Pipedrive: Activity (with subtypes)
    - Zoho: Task, Call, Meeting, Event
    """

    TASK = "TASK"  # Generic to-do item
    TODO = "TODO"  # Alias for TASK (HubSpot compatibility)
    EMAIL = "EMAIL"  # Email activity (logged or tracked)
    CALL = "CALL"  # Phone call (logged or scheduled)
    MEETING = "MEETING"  # Meeting/event with attendees
    NOTE = "NOTE"  # Free-form note attached to records
    EVENT = "EVENT"  # Calendar event (Salesforce/Zoho)
    SMS = "SMS"  # SMS/text message
    LINKEDIN_MESSAGE = "LINKEDIN_MESSAGE"  # LinkedIn InMail/message
    CHAT = "CHAT"  # Live chat transcript
    OTHER = "OTHER"  # Provider-specific types not mapped above


class CRMActivityPriority(StrEnum):
    """Canonical activity priority values across CRM providers."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class CRMActivityObjectType(StrEnum):
    """Object types that activities can be related to.

    Maps to polymorphic `related_object_type` for flexible associations.
    """

    DEAL = "DEAL"
    CONTACT = "CONTACT"
    COMPANY = "COMPANY"
    USER = "USER"
    LEAD = "LEAD"  # Salesforce/Zoho
    ACCOUNT = "ACCOUNT"  # Alias for COMPANY (Salesforce)
    OPPORTUNITY = "OPPORTUNITY"  # Alias for DEAL (Salesforce)


class CRMActivitySource(StrEnum):
    """Origin of the activity record."""

    MANUAL = "MANUAL"  # User-created via UI/API
    CRM_SYNC = "CRM_SYNC"  # Synced from HubSpot/Pipedrive
    AI_MEETING = "AI_MEETING"  # Extracted from meeting transcript
    AI_INSIGHT = "AI_INSIGHT"  # Suggested by deal analysis


# Backwards compatibility aliases
CRMTaskStatus = CRMActivityStatus
CRMTaskType = CRMActivityType
CRMTaskPriority = CRMActivityPriority


class CRMActivityParticipantPreview(BaseModel):
    """Small participant shape used only for activity-card display payloads."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str | None = None
    email: str | None = None
    role: str | None = None


class CRMActivityEmailDisplayPayload(BaseModel):
    """Typed display payload for email activity cards."""

    model_config = ConfigDict(extra="forbid", strict=True)

    association_id: UUID | None = None
    direction: str | None = None
    participants_preview: list[CRMActivityParticipantPreview] = PydanticField(
        default_factory=list,
        max_length=5,
    )
    participant_count: int | None = PydanticField(default=None, ge=0)
    has_attachments: bool | None = None


class CRMActivityMeetingDisplayPayload(BaseModel):
    """Typed display payload for meeting activity cards."""

    model_config = ConfigDict(extra="forbid", strict=True)

    association_id: UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    participants_preview: list[CRMActivityParticipantPreview] = PydanticField(
        default_factory=list,
        max_length=5,
    )
    participant_count: int | None = PydanticField(default=None, ge=0)
    duration_minutes: int | None = PydanticField(default=None, ge=0)
    meeting_status: str | None = None


CRM_ACTIVITY_DISPLAY_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    CRMActivityType.EMAIL.value: CRMActivityEmailDisplayPayload,
    CRMActivityType.MEETING.value: CRMActivityMeetingDisplayPayload,
}


def validate_crm_activity_display_payload(
    activity_type: str | CRMActivityType | None,
    display_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate known activity display payloads before writing the JSONB snapshot."""

    payload = display_payload or {}
    activity_type_value = (
        activity_type.value if isinstance(activity_type, CRMActivityType) else activity_type
    )
    payload_model = CRM_ACTIVITY_DISPLAY_PAYLOAD_MODELS.get(activity_type_value)
    if payload_model is None:
        return payload

    return payload_model.model_validate(payload).model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )


class CRMActivity(FlexibleSyncedRecordMixin, table=True):
    """Canonical activity record across CRMs.

    Supports activities from CRM providers (tasks, calls, meetings, emails, notes)
    and can be linked to any CRM object type via polymorphic relationships.

    Key features:
    - Generic activity types covering all major CRM activity concepts
    - Polymorphic relationships via `related_object_type` + `related_local_id`
    - Backwards-compatible `deal_id` for deal-specific queries
    - Attendee/participant storage in `attendees` JSONB
    - Rich metadata in `properties` and `metrics`

    Migration note: This replaces the former `crm_deal_tasks` table, with the
    table renamed to `crm_activities` and new polymorphic columns added.

    Legacy-retirement note: task/note/detail and direct object-link columns are
    kept only for existing readers/writers during migration. New code should
    write full task/note data to `crm_tasks`/`crm_notes` and object visibility
    to `crm_activity_links`. Remove the legacy columns after backfill, dual-read
    validation, and one safe release cycle prove no production code depends on them.
    """

    __tablename__: ClassVar[str] = "crm_activities"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "activity_id",
            name="uq_crm_activity_org_activity",
        ),
        # Legacy deal-focused indexes (renamed from deal_tasks)
        Index("ix_crm_activity_deal", "deal_id"),
        Index("ix_crm_activity_org_status", "organisation_id", "status"),
        Index("ix_crm_activity_org_type", "organisation_id", "activity_type"),
        Index("ix_crm_activity_org_priority", "organisation_id", "priority"),
        Index("ix_crm_activity_due_date", "due_date"),
        Index("ix_crm_activity_org_due", "organisation_id", "due_date"),
        Index("ix_crm_activity_status_due", "status", "due_date"),
        Index("ix_crm_activity_synced", "organisation_id", "synced_at"),
        Index("ix_crm_activity_org_occurred", "organisation_id", "occurred_at"),
        # New polymorphic relationship indexes
        Index(
            "ix_crm_activity_related",
            "organisation_id",
            "related_object_type",
            "related_local_id",
        ),
        Index("ix_crm_activity_related_local", "related_local_id"),
        # Contact/company specific lookups
        Index("ix_crm_activity_contact", "contact_id"),
        Index("ix_crm_activity_company", "company_id"),
        # Source and creator tracking
        Index("ix_crm_activity_source", "organisation_id", "source"),
        Index("ix_crm_activity_created_by", "organisation_id", "created_by_user_id"),
        Index(
            "ix_crm_activity_task_owner_due",
            "organisation_id",
            "owner_id",
            "due_date",
            postgresql_where=text(
                "is_deleted IS NOT TRUE AND activity_type IN ('TASK', 'TODO') "
                "AND external_assignee_email IS NULL AND external_assignee_name IS NULL"
            ),
        ),
        Index(
            "ix_crm_activity_task_created_by_due",
            "organisation_id",
            "created_by_user_id",
            "due_date",
            postgresql_where=text("is_deleted IS NOT TRUE AND activity_type IN ('TASK', 'TODO')"),
        ),
        Index(
            "ix_crm_activity_task_external_email_due",
            "organisation_id",
            "external_assignee_email",
            "due_date",
            postgresql_where=text(
                "is_deleted IS NOT TRUE AND activity_type IN ('TASK', 'TODO') "
                "AND external_assignee_email IS NOT NULL"
            ),
        ),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Identity
    # ─────────────────────────────────────────────────────────────────────────
    activity_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the activity record.",
    )
    # ─────────────────────────────────────────────────────────────────────────
    # Polymorphic relationship (generic - can link to any object)
    # ─────────────────────────────────────────────────────────────────────────
    related_object_type: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Type of CRM object this activity is related to (DEAL, CONTACT, COMPANY, etc.).",
    )
    related_local_id: UUID | None = Field(
        default=None,
        description="Internal UUID of the related object (polymorphic, not FK-enforced).",
    )
    related_name: str | None = Field(
        default=None,
        sa_column=Column(String(500)),
        description="Denormalized name of the related object for display.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Legacy direct object links. Prefer crm_activity_links for new reads/writes.
    # Drop after the activity-link backfill and compatibility window are complete.
    # ─────────────────────────────────────────────────────────────────────────
    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Internal deal ID (FK) this activity is associated with.",
    )
    contact_id: UUID | None = Field(
        default=None,
        foreign_key="crm_contacts.contact_id",
        description="Internal contact ID (FK) this activity is associated with.",
    )
    company_id: UUID | None = Field(
        default=None,
        foreign_key="crm_companies.company_id",
        description="Internal company ID (FK) this activity is associated with.",
    )
    # ─────────────────────────────────────────────────────────────────────────
    # Core activity fields
    # ─────────────────────────────────────────────────────────────────────────
    subject: str | None = Field(
        default=None,
        sa_column=Column(String(500)),
        description="Activity subject or title.",
    )
    title: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Feed-card title. Source records keep the full canonical data.",
    )
    summary: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Short AI or system generated summary for timeline display.",
    )
    preview: str | None = Field(
        default=None,
        sa_column=Column(String(1000), nullable=True),
        description="Truncated human-readable preview safe for list views.",
    )
    occurred_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp used to order the unified activity feed.",
    )
    source_type: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Canonical source kind: EMAIL, MEETING, TASK, NOTE, RISK, CALL, etc.",
    )
    source_record_id: str | None = Field(
        default=None,
        sa_column=Column(
            Text,
            nullable=True,
            comment="local source row ID for drill-down hydration; provider IDs live in mappings/source tables.",
        ),
        description="Local source row ID for drill-down hydration.",
    )
    source_provider: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="External system that produced the source record, when applicable.",
    )
    source_integration_id: UUID | None = Field(
        default=None,
        foreign_key="knowledge_integrations.integration_id",
        description="Integration that produced the source record, when distinct from this row.",
    )
    source_url: str | None = Field(
        default=None,
        sa_column=Column(String(2048), nullable=True),
        description="Provider URL for the source activity when available.",
    )
    actor_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name of the person, system, or AI actor shown on the feed card.",
    )
    actor_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Actor email for feed display when the actor is a person.",
    )
    display_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
            comment=(
                "Small timeline display snapshot; validate known shapes with Pydantic "
                "before writing."
            ),
        ),
        description=(
            "Small event-specific display snapshot used to render timeline cards without "
            "source-table joins. Known shapes are validated with Pydantic before writing."
        ),
    )
    body: str | None = Field(
        default=None,
        description="Activity description, notes, or content body.",
    )
    status: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Activity status (NOT_STARTED, IN_PROGRESS, COMPLETED, etc.).",
    )
    activity_type: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Activity type (TASK, CALL, MEETING, EMAIL, NOTE, etc.).",
    )
    priority: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Activity priority (LOW, MEDIUM, HIGH, URGENT, NONE).",
    )

    # Provider-specific activity subtype (e.g., Pipedrive activity type name)
    activity_subtype: str | None = Field(
        default=None,
        sa_column=Column(String(100), nullable=True),
        description="Provider-specific activity subtype or category.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Assignment / Ownership
    # ─────────────────────────────────────────────────────────────────────────
    owner_name: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Display name of the activity owner.",
    )
    owner_email: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Email address of the activity owner.",
    )
    owner_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="Resolved internal user ID for the activity owner (FK to crm_users).",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # External assignment (non-app users)
    # ─────────────────────────────────────────────────────────────────────────
    external_assignee_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Email of an external (non-app) person this task is assigned to.",
    )
    external_assignee_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name of the external assignee.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Source tracking
    # ─────────────────────────────────────────────────────────────────────────
    source: str | None = Field(
        default=None,
        sa_column=Column(
            String(50),
            nullable=True,
            comment="Origin of the task: MANUAL, CRM_SYNC, AI_MEETING, AI_INSIGHT",
        ),
        description="How this task was created.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Creator tracking (distinct from owner/assignee)
    # ─────────────────────────────────────────────────────────────────────────
    created_by_user_id: UUID | None = Field(
        default=None,
        description="The public.users ID of who created this task. Immutable after creation.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Timing / Scheduling
    # ─────────────────────────────────────────────────────────────────────────
    due_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Activity due date/time (for tasks) or start time (for events).",
    )
    end_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Activity end time (for meetings/events with duration).",
    )
    completed_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Date/time when the activity was completed.",
    )
    reminder_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Reminder date/time for the activity.",
    )

    # Duration (for calls, meetings)
    duration_minutes: int | None = Field(
        default=None,
        description="Duration of the activity in minutes (calls, meetings).",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Attendees / Participants (meetings, calls)
    # ─────────────────────────────────────────────────────────────────────────
    attendees: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'[]'::jsonb"),
        ),
        description="List of attendees/participants [{email, name, response_status}].",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Communication-specific fields (emails, calls)
    # ─────────────────────────────────────────────────────────────────────────
    direction: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Direction of communication: INBOUND, OUTBOUND, or null.",
    )
    call_outcome: str | None = Field(
        default=None,
        sa_column=Column(String(100), nullable=True),
        description="Outcome of a call (e.g., CONNECTED, LEFT_VOICEMAIL, NO_ANSWER).",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Provider metadata
    # ─────────────────────────────────────────────────────────────────────────
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Original creation timestamp recorded in the provider.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the provider.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Sync metadata
    # ─────────────────────────────────────────────────────────────────────────
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this activity row was last synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this activity was observed during sync.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Flags
    # ─────────────────────────────────────────────────────────────────────────
    is_all_day: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Whether this is an all-day event (meetings/events).",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Payloads
    # ─────────────────────────────────────────────────────────────────────────
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Flattened provider-specific activity properties.",
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Derived metrics for analytics (e.g., response time, overdue status).",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Backwards compatibility aliases
    # ─────────────────────────────────────────────────────────────────────────
    @property
    def is_deleted_remote(self) -> bool:
        """Backwards-compatible alias for SyncedRecordMixin.is_deleted."""

        return bool(self.is_deleted)

    @is_deleted_remote.setter
    def is_deleted_remote(self, value: bool) -> None:
        """Backwards-compatible setter alias for is_deleted updates."""
        self.is_deleted = bool(value)

    @property
    def task_id(self) -> UUID:
        """Alias for activity_id (backwards compatibility with CRMDealTask)."""
        return self.activity_id

    @property
    def task_type(self) -> str | None:
        """Alias for activity_type (backwards compatibility with CRMDealTask)."""
        return self.activity_type

    @task_type.setter
    def task_type(self, value: str | None) -> None:
        """Set activity_type via task_type alias."""
        self.activity_type = value


class CRMActivityLink(SQLModel, table=True):
    """Associates one feed activity with any CRM object timeline."""

    __tablename__: ClassVar[str] = "crm_activity_links"
    __table_args__ = (
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="ck_crm_activity_link_confidence_score_range",
        ),
        CheckConstraint(
            "object_type IN ('DEAL', 'COMPANY', 'CONTACT', 'LEAD', 'USER', 'RISK', 'TASK', 'NOTE')",
            name="ck_crm_activity_link_object_type",
        ),
        CheckConstraint(
            "link_role IN ("
            "'PRIMARY', 'SENDER', 'RECIPIENT', 'ATTENDEE', 'OWNER', 'MENTIONED', "
            "'RELATED', 'CREATED', 'RESOLVED', 'ASSIGNEE'"
            ")",
            name="ck_crm_activity_link_role",
        ),
        CheckConstraint(
            "link_source IN ('MANUAL', 'PROVIDER_ASSOCIATION', 'RULE_MATCH', 'AI_MATCH', 'SYSTEM')",
            name="ck_crm_activity_link_source",
        ),
        ForeignKeyConstraint(
            ["organisation_id", "activity_id"],
            ["crm_activities.organisation_id", "crm_activities.activity_id"],
            ondelete="CASCADE",
            name="fk_crm_activity_links_org_activity",
        ),
        UniqueConstraint(
            "organisation_id",
            "activity_id",
            "object_type",
            "object_id",
            "link_role",
            name="uq_crm_activity_link_target_role",
        ),
        Index(
            "ix_crm_activity_links_object_time",
            "organisation_id",
            "object_type",
            "object_id",
            "occurred_at",
            "activity_id",
        ),
        Index("ix_crm_activity_links_activity", "organisation_id", "activity_id"),
    )

    activity_link_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    activity_id: UUID = Field(
        sa_column=Column(
            Uuid,
            nullable=False,
            index=True,
        )
    )
    object_type: str = Field(sa_column=Column(String(32), nullable=False))
    object_id: UUID = Field(index=True)
    link_role: str = Field(
        default="RELATED",
        sa_column=Column(String(64), nullable=False, server_default=text("'RELATED'")),
        description="Relationship role such as PRIMARY, RELATED, MENTIONED, ASSIGNEE, or OWNER.",
    )
    link_source: str = Field(
        default="SYSTEM",
        sa_column=Column(String(64), nullable=False, server_default=text("'SYSTEM'")),
        description="How the link was created: MANUAL, PROVIDER_ASSOCIATION, RULE_MATCH, AI_MATCH, SYSTEM.",
    )
    confidence_score: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 4), nullable=True),
        description="Confidence for inferred links. Direct links can leave this empty.",
    )
    occurred_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Explicit copy of the source activity timestamp for object timeline ordering.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )


class CRMTask(FlexibleSyncedRecordMixin, table=True):
    """Canonical task source record used by the feed activity projection.

    This table stores current task state only. Reassignment, due-date changes,
    completion, reopening, and other lifecycle transitions should be recorded in
    crm_mutation_events or a dedicated task-event stream, not by relying only on
    the latest row values here.
    """

    __tablename__: ClassVar[str] = "crm_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')",
            name="ck_crm_task_status",
        ),
        CheckConstraint(
            "priority IN ('NONE', 'LOW', 'MEDIUM', 'HIGH', 'URGENT')",
            name="ck_crm_task_priority",
        ),
        Index("ix_crm_task_org_due", "organisation_id", "due_at", "task_id"),
        Index("ix_crm_task_org_due_date", "organisation_id", "due_date", "task_id"),
        Index(
            "ix_crm_task_deal",
            "organisation_id",
            "deal_id",
            "due_at",
            "task_id",
            postgresql_where=text("deal_id IS NOT NULL AND is_deleted IS NOT TRUE"),
        ),
        Index("ix_crm_task_assignee_due", "organisation_id", "assigned_to_user_id", "due_at"),
        Index("ix_crm_task_status_due", "organisation_id", "status", "due_at"),
    )

    task_id: UUID = Field(default_factory=uuid4, primary_key=True)
    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        description="Primary deal context for fast deal-scoped task reads.",
    )
    title: str = Field(sa_column=Column(String(500), nullable=False))
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status: str = Field(
        default="NOT_STARTED",
        sa_column=Column(String(32), nullable=False, server_default=text("'NOT_STARTED'")),
    )
    priority: str = Field(
        default="NONE",
        sa_column=Column(String(32), nullable=False, server_default=text("'NONE'")),
    )
    due_date: date | None = Field(
        default=None,
        sa_column=Column(Date, nullable=True),
        description="Date-only due value for CRM tasks that are not due at a specific instant.",
    )
    due_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    is_all_day: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    reminder_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    assigned_to_user_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="Resolved CRM user assigned to the task, when known.",
    )
    assigned_to_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    assigned_to_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    created_by_user_id: UUID | None = Field(
        default=None,
        description="Public user or workflow actor that created this task.",
    )
    owner_user_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="CRM owner accountable for the task record, distinct from assignee.",
    )
    owner_email: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    completed_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="Resolved CRM user who completed the task, when known.",
    )
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    task_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )

    @property
    def activity_id(self) -> UUID:
        """Backwards-compatible alias while task APIs move off crm_activities."""
        return self.task_id

    @property
    def subject(self) -> str | None:
        return self.title

    @property
    def body(self) -> str | None:
        return self.description

    @property
    def activity_type(self) -> str:
        metadata = self.task_metadata or {}
        task_type = metadata.get("task_type")
        return task_type if task_type in {"TASK", "TODO"} else "TASK"

    @property
    def task_type(self) -> str:
        return self.activity_type

    @property
    def reminder_date(self) -> datetime | None:
        return self.reminder_at

    @property
    def completed_date(self) -> datetime | None:
        return self.completed_at

    @property
    def owner_id(self) -> UUID | None:
        return self.assigned_to_user_id

    @property
    def owner_name(self) -> str | None:
        if (self.task_metadata or {}).get("assignee_kind") == "external":
            return None
        return self.assigned_to_name

    @property
    def external_assignee_email(self) -> str | None:
        if (self.task_metadata or {}).get("assignee_kind") == "external":
            return self.assigned_to_email
        return None

    @property
    def external_assignee_name(self) -> str | None:
        if (self.task_metadata or {}).get("assignee_kind") == "external":
            return self.assigned_to_name
        return None

    @property
    def properties(self) -> dict[str, Any]:
        return self.task_metadata or {}

    @property
    def metrics(self) -> None:
        return None

    @property
    def source(self) -> str | None:
        value = (self.task_metadata or {}).get("source")
        return value if isinstance(value, str) else None

    @property
    def synced_at(self) -> datetime:
        return self.source_modified_at or self.updated_at

    @property
    def last_seen_at(self) -> datetime | None:
        return self.source_modified_at

    @property
    def is_deleted_remote(self) -> bool:
        return bool(self.is_deleted)


class CRMNote(FlexibleSyncedRecordMixin, table=True):
    """Canonical note source record used by the feed activity projection."""

    __tablename__: ClassVar[str] = "crm_notes"
    __table_args__ = (
        CheckConstraint(
            "body_format IN ('plain_text', 'markdown', 'html')",
            name="ck_crm_note_body_format",
        ),
        CheckConstraint(
            "visibility IN ('PRIVATE', 'TEAM', 'ORG')",
            name="ck_crm_note_visibility",
        ),
        Index("ix_crm_note_author", "organisation_id", "author_user_id"),
        Index(
            "ix_crm_note_deal",
            "organisation_id",
            "deal_id",
            "created_at",
            postgresql_where=text("deal_id IS NOT NULL AND is_deleted IS NOT TRUE"),
        ),
        Index("ix_crm_note_visibility", "organisation_id", "visibility"),
    )

    note_id: UUID = Field(default_factory=uuid4, primary_key=True)
    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        description="Primary deal context for fast deal-scoped note reads.",
    )
    title: str | None = Field(default=None, sa_column=Column(String(500), nullable=True))
    body: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Full note body. Feed rows keep only summary/preview text.",
    )
    body_format: str = Field(
        default="plain_text",
        sa_column=Column(String(32), nullable=False, server_default=text("'plain_text'")),
    )
    author_user_id: UUID | None = Field(default=None, foreign_key="crm_users.user_id")
    author_name: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    author_email: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    visibility: str = Field(
        default="TEAM",
        sa_column=Column(String(32), nullable=False, server_default=text("'TEAM'")),
    )
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    note_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )

    @property
    def activity_id(self) -> UUID:
        """Backwards-compatible alias while note APIs move off crm_activities."""
        return self.note_id

    @property
    def subject(self) -> str | None:
        return self.title

    @property
    def owner_name(self) -> str | None:
        return self.author_name

    @property
    def owner_email(self) -> str | None:
        return self.author_email

    @property
    def owner_id(self) -> UUID | None:
        return self.author_user_id

    @property
    def properties(self) -> dict[str, Any]:
        return self.note_metadata or {}

    @property
    def synced_at(self) -> datetime:
        return self.source_modified_at or self.updated_at

    @property
    def last_seen_at(self) -> datetime | None:
        return self.source_modified_at


# Backwards compatibility alias for existing code
CRMDealTask = CRMActivity
