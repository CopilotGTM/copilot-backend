"""Generic notification delivery audit rows.

Notification services use this table as a reusable delivery ledger: a worker
claims one `(notification, source, channel, target)` row before sending, then
marks it sent or failed. That gives email, Slack, Teams, and future channels the
same retry/idempotency behavior instead of creating feature-specific tables.
"""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync.integration_models import EMPTY_JSONB_SQL


class NotificationDeliveryChannel(str, Enum):
    """Supported outbound channel names persisted in the delivery ledger."""

    EMAIL = "email"
    SLACK = "slack"
    TEAMS = "teams"
    WHATSAPP = "whatsapp"
    GOOGLE_CHAT = "google_chat"


class NotificationDeliveryStatus(str, Enum):
    """Lifecycle states for a claimed notification target."""

    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    # The provider result is uncertain or needs a deliberate repair, so automatic
    # retries must stop without changing the completed source run.
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class NotificationDelivery(SQLModel, table=True):
    """One durable row per notification target across all outbound channels.

    The unique constraint is the idempotency boundary. Retried workers can only
    claim one row for the same source/channel/target, which prevents duplicate
    notifications when Celery retries or two dispatchers race.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "notification_type",
            "source_type",
            "source_id",
            "channel",
            "target_key",
            name="uq_notification_delivery_target",
        ),
        Index("ix_notification_delivery_org_status", "organisation_id", "status"),
        Index("ix_notification_delivery_source", "source_type", "source_id"),
        Index("ix_notification_delivery_type_channel", "notification_type", "channel"),
        Index(
            "ix_notification_delivery_recovery",
            "organisation_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_notification_delivery_stale_claim",
            "organisation_id",
            "status",
            "claimed_at",
        ),
    )

    delivery_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    notification_type: str = Field(sa_column=Column(String(64), nullable=False))
    source_type: str = Field(sa_column=Column(String(64), nullable=False))
    source_id: UUID = Field(index=True)
    channel: NotificationDeliveryChannel = Field(sa_column=Column(String(32), nullable=False))
    target_key: str = Field(sa_column=Column(String(255), nullable=False))
    target_label: str | None = Field(default=None, sa_column=Column(String(320), nullable=True))
    status: NotificationDeliveryStatus = Field(
        default=NotificationDeliveryStatus.PENDING,
        sa_column=Column(String(32), nullable=False, server_default=text("'PENDING'")),
    )
    attempt_count: int = Field(default=0, nullable=False)
    # Claim metadata fences concurrent workers and lets the recovery sweep reclaim
    # only pending rows or sending leases that have become stale.
    next_attempt_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    claimed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    claimed_by: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    claim_token: UUID | None = Field(default=None, sa_column=Column(Uuid, nullable=True))
    # Provider and error metadata belongs to the delivery ledger, not AgentRun;
    # the source run remains a durable record of execution success or failure.
    provider_resource_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    error_code: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    sent_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    delivery_metadata: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
