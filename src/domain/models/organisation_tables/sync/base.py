from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ..base_mixins import TimestampMixin, utcnow


class SyncCategory(str, Enum):
    CALENDAR = "calendar"
    CRM = "crm"
    EMAIL = "email"


class SyncedRecordMixin(TimestampMixin):
    """Common columns shared across synced data tables."""

    organisation_id: UUID = Field(index=True)
    integration_id: UUID = Field(
        index=True,
        foreign_key="knowledge_integrations.integration_id",
        description="Integration owning this record.",
    )
    external_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider identifier (per integration).",
    )
    data_payload: dict | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider payload for traceability.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this row was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Provider's last modified timestamp, if supplied.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Latest time this record was observed in provider feed.",
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft delete flag reflecting provider state.",
    )
