from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from .base_mixins import TimestampMixin

# SQL server defaults
_DEFAULT_STATUS_ACTIVE = text("'active'")
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


class UserReminder(TimestampMixin, table=True):
    """
    A user-created reminder or scheduled alert.

    STATIC reminders deliver a fixed message at the trigger time.
    AGENTIC reminders run the GeneralAgent with a stored prompt and deliver the result.

    Created via agent tools in the Teams channel; scheduled via Redis sorted set
    (ZSET timing index) and executed by the chat_worker poll_due_reminders task.
    """

    __tablename__ = "user_reminders"
    __table_args__ = (
        Index("ix_user_reminders_org_status", "organisation_id", "status"),
        Index("ix_user_reminders_next_trigger", "next_trigger_at", "status"),
        Index("ix_user_reminders_user_status", "user_id", "status"),
    )

    # Identity
    reminder_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    user_id: UUID = Field(index=True)

    # Type & content
    reminder_type: ReminderType = Field(
        sa_column=Column(String(20), nullable=False),
    )
    title: str = Field(max_length=255)
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
    )
    user_timezone: str = Field(
        default="UTC",
        max_length=50,
        description="IANA timezone, e.g. 'America/New_York'.",
    )
    recurrence: ReminderRecurrence = Field(
        sa_column=Column(String(20), server_default=_DEFAULT_RECURRENCE_NONE, nullable=False),
        default=ReminderRecurrence.NONE,
    )
    recurrence_end_at: datetime | None = Field(
        default=None,
        description="Stop recurring after this UTC datetime.",
    )

    # Delivery
    delivery_channel: str = Field(
        sa_column=Column(String(20), server_default=_DEFAULT_CHANNEL_TEAMS, nullable=False),
        default="teams",
    )
    delivery_context: dict[str, Any] = Field(
        sa_column=Column(JSONB, server_default=_DEFAULT_EMPTY_JSONB, nullable=False),
        default_factory=dict,
        description="Channel-specific delivery info: {service_url, conversation_id, ...}.",
    )

    # Status & tracking
    status: ReminderStatus = Field(
        default=ReminderStatus.ACTIVE,
        sa_column=Column(String(20), server_default=_DEFAULT_STATUS_ACTIVE, nullable=False),
    )
    last_triggered_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True),
        default=None,
    )
    trigger_count: int = Field(
        sa_column=Column(Integer, server_default=text("0"), nullable=False),
        default=0,
    )
    consecutive_failures: int = Field(
        sa_column=Column(Integer, server_default=text("0"), nullable=False),
        default=0,
    )
    last_error: str | None = Field(default=None)


UserReminder.update_forward_refs()
