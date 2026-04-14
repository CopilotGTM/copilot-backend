from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..base import SyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMUser(SyncedRecordMixin, table=True):
    """Normalised CRM user / owner profile."""

    __tablename__ = "crm_users"
    __table_args__ = (
        Index(
            "uq_crm_user_external",
            "integration_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("ix_crm_user_org_email", "organisation_id", "email"),
        Index("ix_crm_user_active", "organisation_id", "is_active"),
    )

    # Identity
    user_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the normalised user record.",
    )
    external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider-supplied unique identifier for the user. Null for internal-only CRM users.",
    )

    # Primary details
    email: str | None = Field(
        default=None,
        sa_column=Column(String(320), nullable=True),
        description="Primary email associated with the user account.",
    )
    first_name: str | None = Field(default=None, description="User first name.")
    last_name: str | None = Field(default=None, description="User last name.")
    full_name: str | None = Field(default=None, description="Display name for the user.")
    user_role: str | None = Field(default=None, description="Provider role or title.")
    job_title: str | None = Field(default=None, description="Job title captured in the CRM.")
    time_zone: str | None = Field(default=None, description="User configured time zone.")
    locale: str | None = Field(default=None, description="Locale or language preference.")

    # Teams & ownership
    primary_team_id: str | None = Field(
        default=None,
        description="Primary team identifier if supplied by the provider.",
    )
    team_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'[]'::jsonb"),
        ),
        description="List of team identifiers the user belongs to.",
    )

    # Status flags
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
        description="Whether the user is active according to the provider.",
    )

    # Activity
    last_login_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of last login or access, when provided.",
    )
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Original creation time reported by the provider.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the provider.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this user row was last synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this user was observed during sync.",
    )

    # Payloads
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Raw provider properties captured for this user.",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider payload useful for debugging.",
    )

    @property
    def is_deleted_remote(self) -> bool:
        """Backwards-compatible alias for SyncedRecordMixin.is_deleted."""

        return bool(self.is_deleted)

    @is_deleted_remote.setter
    def is_deleted_remote(self, value: bool) -> None:
        self.is_deleted = bool(value)
