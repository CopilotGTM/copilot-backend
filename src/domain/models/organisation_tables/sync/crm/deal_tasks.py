"""Canonical CRM task/activity models across providers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..base import SyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMTaskStatus(str, Enum):
    """Canonical task status values across CRM providers."""

    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    WAITING = "WAITING"
    DEFERRED = "DEFERRED"


class CRMTaskType(str, Enum):
    """Canonical task type values across CRM providers."""

    TODO = "TODO"
    EMAIL = "EMAIL"
    CALL = "CALL"
    MEETING = "MEETING"


class CRMTaskPriority(str, Enum):
    """Canonical task priority values across CRM providers."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    NONE = "NONE"


class CRMDealTask(SyncedRecordMixin, table=True):
    """Canonical deal task record across CRMs.

    Supports both synced tasks from CRM providers and internally-created tasks.
    """

    __tablename__ = "crm_deal_tasks"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_id", name="uq_crm_deal_task_external"),
        Index("ix_crm_deal_task_deal", "deal_id"),
        Index("ix_crm_deal_task_org_status", "organisation_id", "status"),
        Index("ix_crm_deal_task_org_type", "organisation_id", "task_type"),
        Index("ix_crm_deal_task_org_priority", "organisation_id", "priority"),
        Index("ix_crm_deal_task_owner", "owner_external_id"),
        Index("ix_crm_deal_task_due_date", "due_date"),
        Index("ix_crm_deal_task_org_due", "organisation_id", "due_date"),
        Index("ix_crm_deal_task_status_due", "status", "due_date"),
        Index("ix_crm_deal_task_synced", "organisation_id", "synced_at"),
    )

    # Identity
    task_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the task record.",
    )
    external_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider-supplied unique identifier for the task.",
    )

    # Deal linkage
    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Internal deal ID (FK) this task is associated with.",
    )
    deal_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider deal ID this task is associated with.",
    )

    # Core task fields
    subject: str | None = Field(
        default=None,
        sa_column=Column(String(500)),
        description="Task subject or title.",
    )
    body: str | None = Field(
        default=None,
        description="Task description or notes.",
    )
    status: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Task status (NOT_STARTED, IN_PROGRESS, COMPLETED, WAITING, DEFERRED).",
    )
    task_type: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Task type (TODO, EMAIL, CALL, MEETING).",
    )
    priority: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Task priority (LOW, MEDIUM, HIGH, NONE).",
    )

    # Assignment
    owner_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True, index=True),
        description="Provider identifier for the user/owner assigned to the task.",
    )
    owner_name: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Display name of the task owner.",
    )
    owner_email: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Email address of the task owner, if available.",
    )

    # Timing
    due_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Task due date/time.",
    )
    completed_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Date/time when the task was completed.",
    )
    reminder_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Reminder date/time for the task.",
    )

    # Provider metadata
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

    # Sync metadata
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this task row was last synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this task was observed during sync.",
    )

    # Flags
    is_deleted_remote: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Provider deletion flag for the task.",
    )

    # Payloads
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Flattened provider-specific task properties.",
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Derived metrics for analytics (e.g., completion time, overdue status).",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider payload for troubleshooting.",
    )
