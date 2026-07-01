from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from .base_mixins import TimestampMixin
from .sync.integration_models import EMPTY_JSONB_SQL


class DashboardSnapshot(TimestampMixin, table=True):
    """Tenant-scoped dashboard metadata and latest generated payload."""

    __tablename__ = "dashboards"

    dashboard_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    dashboard_key: str = Field(sa_column=Column(String(128), nullable=False))
    dashboard_name: str = Field(sa_column=Column(String(255), nullable=False))
    dashboard_type: str = Field(sa_column=Column(String(64), nullable=False))
    schema_version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    definition: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
    )
    latest_data: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    latest_data_generated_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    latest_generation_status: str = Field(
        default="pending",
        sa_column=Column(String(32), nullable=False, server_default=text("'pending'")),
    )
    latest_generation_error: str | None = Field(default=None, sa_column=Column(String))
    refresh_interval_seconds: int = Field(
        default=86400,
        sa_column=Column(Integer, nullable=False, server_default=text("86400")),
    )
    created_by_user_id: UUID | None = Field(default=None, index=True)
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
