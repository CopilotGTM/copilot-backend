"""Canonical CRM activity models across providers.

This module provides a generalized activity model that can represent various
CRM activity types (tasks, calls, meetings, emails, notes) and link them to
any CRM object (deals, contacts, companies) via polymorphic relationships.

Replaces the deal-specific CRMDealTask model with a more flexible approach
that better supports multi-CRM integration patterns.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..base import FlexibleSyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMActivityStatus(str, Enum):
    """Canonical activity status values across CRM providers."""

    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    WAITING = "WAITING"
    DEFERRED = "DEFERRED"
    CANCELLED = "CANCELLED"


class CRMActivityType(str, Enum):
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


class CRMActivityPriority(str, Enum):
    """Canonical activity priority values across CRM providers."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class CRMActivityObjectType(str, Enum):
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


class CRMActivitySource(str, Enum):
    """Origin of the activity record."""

    MANUAL = "MANUAL"  # User-created via UI/API
    CRM_SYNC = "CRM_SYNC"  # Synced from HubSpot/Pipedrive
    AI_MEETING = "AI_MEETING"  # Extracted from meeting transcript
    AI_INSIGHT = "AI_INSIGHT"  # Suggested by deal analysis


# Backwards compatibility aliases
CRMTaskStatus = CRMActivityStatus
CRMTaskType = CRMActivityType
CRMTaskPriority = CRMActivityPriority


class CRMActivity(FlexibleSyncedRecordMixin, table=True):
    """Canonical activity record across CRMs.

    Supports activities from CRM providers (tasks, calls, meetings, emails, notes)
    and can be linked to any CRM object type via polymorphic relationships.

    Key features:
    - Generic activity types covering all major CRM activity concepts
    - Polymorphic relationships via `related_object_type` + `related_external_id`
    - Backwards-compatible `deal_id`/`deal_external_id` for deal-specific queries
    - Attendee/participant storage in `attendees` JSONB
    - Rich metadata in `properties`, `metrics`, `data_payload`
    - Internal-only activities may leave `external_id` null until mirrored to a provider

    Migration note: This replaces the former `crm_deal_tasks` table, with the
    table renamed to `crm_activities` and new polymorphic columns added.
    """

    __tablename__ = "crm_activities"
    __table_args__ = (
        # Unique constraint for CRM-synced activities (integration_id NOT NULL)
        Index(
            "uq_crm_activity_external",
            "integration_id",
            "external_id",
            unique=True,
            postgresql_where=text("integration_id IS NOT NULL"),
        ),
        # Unique constraint for internal/user-created activities when an external ID exists
        Index(
            "uq_crm_activity_external_user",
            "organisation_id",
            "external_id",
            unique=True,
            postgresql_where=text("integration_id IS NULL"),
        ),
        # Legacy deal-focused indexes (renamed from deal_tasks)
        Index("ix_crm_activity_deal", "deal_id"),
        Index("ix_crm_activity_org_status", "organisation_id", "status"),
        Index("ix_crm_activity_org_type", "organisation_id", "activity_type"),
        Index("ix_crm_activity_org_priority", "organisation_id", "priority"),
        Index("ix_crm_activity_owner", "owner_external_id"),
        Index("ix_crm_activity_due_date", "due_date"),
        Index("ix_crm_activity_org_due", "organisation_id", "due_date"),
        Index("ix_crm_activity_status_due", "status", "due_date"),
        Index("ix_crm_activity_synced", "organisation_id", "synced_at"),
        # New polymorphic relationship indexes
        Index(
            "ix_crm_activity_related",
            "organisation_id",
            "related_object_type",
            "related_external_id",
        ),
        Index("ix_crm_activity_related_local", "related_local_id"),
        # Contact/company specific lookups
        Index("ix_crm_activity_contact", "contact_id"),
        Index("ix_crm_activity_company", "company_id"),
        # Source and creator tracking
        Index("ix_crm_activity_source", "organisation_id", "source"),
        Index("ix_crm_activity_created_by", "organisation_id", "created_by_user_id"),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Identity
    # ─────────────────────────────────────────────────────────────────────────
    activity_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the activity record.",
    )
    external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider-supplied unique identifier for the activity. Null for internal-only activities.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Polymorphic relationship (generic - can link to any object)
    # ─────────────────────────────────────────────────────────────────────────
    related_object_type: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Type of CRM object this activity is related to (DEAL, CONTACT, COMPANY, etc.).",
    )
    related_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider external ID of the related object.",
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
    # Specific object links (for common query patterns and FK integrity)
    # ─────────────────────────────────────────────────────────────────────────
    # Deal linkage (backwards compatible with former crm_deal_tasks)
    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Internal deal ID (FK) this activity is associated with.",
    )
    deal_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider deal ID this activity is associated with.",
    )

    # Contact linkage
    contact_id: UUID | None = Field(
        default=None,
        foreign_key="crm_contacts.contact_id",
        description="Internal contact ID (FK) this activity is associated with.",
    )
    contact_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider contact ID this activity is associated with.",
    )

    # Company linkage
    company_id: UUID | None = Field(
        default=None,
        foreign_key="crm_companies.company_id",
        description="Internal company ID (FK) this activity is associated with.",
    )
    company_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider company ID this activity is associated with.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Core activity fields
    # ─────────────────────────────────────────────────────────────────────────
    subject: str | None = Field(
        default=None,
        sa_column=Column(String(500)),
        description="Activity subject or title.",
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
    owner_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True, index=True),
        description="Provider identifier for the user/owner assigned to the activity.",
    )
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
        description="List of attendees/participants [{email, name, external_id, response_status}].",
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
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider payload for troubleshooting.",
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


# Backwards compatibility alias for existing code
CRMDealTask = CRMActivity
