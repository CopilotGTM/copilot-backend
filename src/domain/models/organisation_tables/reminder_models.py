from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel

from .base_mixins import TimestampMixin

# SQL server defaults
_DEFAULT_STATUS_ACTIVE = text("'active'")
_DEFAULT_RUN_STATUS_IN_PROGRESS = text("'in_progress'")
_DEFAULT_EMPTY_JSONB = text("'{}'::jsonb")
_DEFAULT_RECURRENCE_NONE = text("'none'")
_DEFAULT_CHANNEL_TEAMS = text("'teams'")


class ReminderType(str, Enum):
    """Whether the reminder delivers a static message or runs an agent."""

    STATIC = "static"
    AGENTIC = "agentic"


class ReminderRecurrence(str, Enum):
    """Recurrence pattern for a reminder."""

    NONE = "none"
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


class ReminderStatus(str, Enum):
    """Lifecycle status of a reminder."""

    PENDING_CONFIRMATION = "pending_confirmation"
    ACTIVE = "active"
    PAUSED = "paused"
    TRIGGERED = "triggered"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReminderRunStatus(str, Enum):
    """Execution status for an individual reminder run."""

    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    DELIVERY_FAILED = "delivery_failed"
    FAILED = "failed"


class UserReminder(TimestampMixin, table=True):
    """
    A user-created reminder or scheduled alert.

    STATIC reminders deliver a fixed message at the trigger time.
    AGENTIC reminders run the GeneralAgent with a stored prompt and deliver the result.

    Created via agent tools in the Teams channel; scheduled via Redis sorted set
    (ZSET timing index) and executed by the chat_worker poll_due_reminders task.
    """

    __tablename__: ClassVar[str] = "user_reminders"
    __table_args__: ClassVar[tuple[Index, ...]] = (
        Index("ix_user_reminders_org_status", "organisation_id", "status"),
        Index("ix_user_reminders_next_trigger", "next_trigger_at", "status"),
        Index("ix_user_reminders_user_status", "user_id", "status"),
    )

    # Identity
    reminder_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for the reminder configuration.",
    )
    organisation_id: UUID = Field(
        index=True,
        description="Organisation boundary for multi-tenant isolation.",
    )
    user_id: UUID = Field(
        index=True,
        description="Owner of the reminder within the organisation.",
    )

    # Type & content
    reminder_type: ReminderType = Field(
        sa_column=Column(String(20), nullable=False),
        description="Whether the reminder is static content or an agent-driven task.",
    )
    title: str = Field(max_length=255, description="Short user-facing label for the reminder.")
    message_content: str | None = Field(
        default=None,
        description="Text to deliver for STATIC reminders.",
    )
    agent_config: dict[str, Any] | None = Field(
        sa_column=Column(JSONB),
        default=None,
        description="For AGENTIC: {agent_type, prompt, deal_id, parameters}.",
    )

    # Schedule
    next_trigger_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Next scheduled fire time in UTC.",
    )
    user_timezone: str = Field(
        default="UTC",
        max_length=50,
        description="IANA timezone, e.g. 'America/New_York'.",
    )
    recurrence: ReminderRecurrence = Field(
        sa_column=Column(String(20), server_default=_DEFAULT_RECURRENCE_NONE, nullable=False),
        default=ReminderRecurrence.NONE,
        description="Recurrence cadence used to compute the next trigger after each run.",
    )
    recurrence_end_at: datetime | None = Field(
        default=None,
        description="Stop recurring after this UTC datetime.",
    )

    # Delivery
    delivery_channel: str = Field(
        sa_column=Column(String(20), server_default=_DEFAULT_CHANNEL_TEAMS, nullable=False),
        default="teams",
        description="Target delivery channel used when the reminder fires.",
    )
    delivery_context: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=_DEFAULT_EMPTY_JSONB, nullable=False),
        default_factory=dict,
        description="Channel-specific delivery info: {service_url, conversation_id, ...}.",
    )

    # Activity linkage (tasks, notes, etc.)
    activity_id: UUID | None = Field(
        default=None,
        sa_column=Column(PG_UUID(as_uuid=True), nullable=True, index=True),
        description="Linked CRM activity ID (crm_activities.activity_id).",
    )

    # Status & tracking
    status: ReminderStatus = Field(
        default=ReminderStatus.ACTIVE,
        sa_column=Column(String(20), server_default=_DEFAULT_STATUS_ACTIVE, nullable=False),
        description="Current lifecycle state of the reminder configuration.",
    )
    last_triggered_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
        description="Timestamp of the last successful execution.",
    )
    trigger_count: int = Field(
        sa_column=Column(Integer, server_default=text("0"), nullable=False),
        default=0,
        description="Count of successful executions, not total attempts.",
    )
    consecutive_failures: int = Field(
        sa_column=Column(Integer, server_default=text("0"), nullable=False),
        default=0,
        description="Number of back-to-back failed attempts since the last success.",
    )
    last_error: str | None = Field(
        default=None,
        description="Most recent execution error message, if any.",
    )


class UserReminderRun(SQLModel, table=True):
    """
    Immutable execution history for a reminder.

    One row is written per execution attempt so the frontend can show a lean,
    complete timeline: when it was supposed to run, what input was executed,
    what output was produced, and whether delivery or agent execution failed.
    """

    __tablename__: ClassVar[str] = "user_reminder_runs"
    __table_args__: ClassVar[tuple[Index, ...]] = (
        Index("ix_user_reminder_runs_org_started", "organisation_id", "started_at"),
        Index("ix_user_reminder_runs_reminder_started", "reminder_id", "started_at"),
        Index("ix_user_reminder_runs_user_started", "user_id", "started_at"),
    )

    run_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Primary key for this execution attempt.",
    )
    organisation_id: UUID = Field(
        index=True,
        description="Organisation boundary copied from the parent reminder for audit consistency.",
    )
    reminder_id: UUID = Field(
        foreign_key="user_reminders.reminder_id",
        index=True,
        description="Parent reminder that this execution attempt belongs to.",
    )
    user_id: UUID = Field(
        index=True,
        description="User who owned the parent reminder when this run executed.",
    )

    attempt_number: int = Field(
        sa_column=Column(Integer, nullable=False),
        description="Sequential execution attempt number for this reminder.",
    )
    triggered_for_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Scheduled trigger time this run was executing for, in UTC.",
    )

    status: ReminderRunStatus = Field(
        default=ReminderRunStatus.IN_PROGRESS,
        sa_column=Column(
            String(20),
            server_default=_DEFAULT_RUN_STATUS_IN_PROGRESS,
            nullable=False,
        ),
        description="Execution result state for this single run attempt.",
    )

    input_snapshot: str = Field(
        default="",
        sa_column=Column(Text, nullable=False),
        description="Exact reminder input executed for this run: static message or agent prompt.",
    )
    result_text: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Human-readable output produced by the run, if any.",
    )
    error_message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Failure reason captured for this run, if any.",
    )

    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
        description="When the execution attempt actually started.",
    )
    completed_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
        description="When the execution attempt finished, if completed.",
    )


UserReminder.update_forward_refs()
UserReminderRun.update_forward_refs()
