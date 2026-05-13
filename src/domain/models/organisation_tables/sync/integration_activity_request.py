"""Reusable audit model for provider activities initiated by agents."""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from src.domain.models.organisation_tables.base_mixins import utcnow


class IntegrationActivityRequestStatus(str, Enum):
    """Lifecycle states for outbound provider activity attempts."""

    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class IntegrationActivityRequest(SQLModel, table=True):
    """Stored audit record for one provider-side activity write operation."""

    __tablename__ = "integration_activity_requests"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "requested_by_user_id",
            "activity_type",
            "idempotency_key",
            name="uq_integration_activity_request_idempotency",
        ),
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED')",
            name="ck_integration_activity_request_status",
        ),
        Index("ix_integration_activity_request_org_status", "organisation_id", "status"),
        Index("ix_integration_activity_request_type_status", "activity_type", "status"),
    )

    request_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    requested_by_user_id: UUID = Field(index=True)
    integration_id: UUID = Field(foreign_key="knowledge_integrations.integration_id", index=True)
    deal_id: UUID | None = Field(default=None, foreign_key="crm_deals.deal_id", index=True)
    linked_record_type: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    linked_record_id: UUID | None = Field(default=None, index=True)
    activity_type: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    idempotency_key: str = Field(sa_column=Column(String(128), nullable=False))
    status: IntegrationActivityRequestStatus = Field(
        default=IntegrationActivityRequestStatus.PENDING,
        sa_column=Column(String(32), nullable=False, server_default=text("'PENDING'")),
    )
    title: str = Field(sa_column=Column(String(998), nullable=False))
    body: str | None = Field(default=None, sa_column=Column(Text))
    location: str | None = Field(default=None, sa_column=Column(String(500)))
    start_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    end_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    timezone: str | None = Field(default=None, sa_column=Column(String(128)))
    provider_timezone: str | None = Field(default=None, sa_column=Column(String(128)))
    participants: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    provider_resource_id: str | None = Field(
        default=None, sa_column=Column(String(255), index=True)
    )
    conference_link: str | None = Field(default=None, sa_column=Column(Text))
    request_payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    provider_status_code: int | None = Field(default=None)
    provider_response: dict | None = Field(default=None, sa_column=Column(JSONB))
    history: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    error_code: str | None = Field(default=None, sa_column=Column(String(128)))
    error_message: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
