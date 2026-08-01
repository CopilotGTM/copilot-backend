"""Canonical participant records shared across synced provider data."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ..tenant_constraints import tenant_foreign_key
from .base import FlexibleSyncedRecordMixin
from .integration_models import EMPTY_JSONB_SQL


class IntegrationParticipant(FlexibleSyncedRecordMixin, table=True):
    """
    Canonical participant record — org-scoped, shared across integrations.

    integration_id is nullable: set to the first integration that discovered
    this participant, but not used for scoping. Provider-specific details are
    kept on the canonical participant record.
    """

    __tablename__ = "integration_participants"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "participant_id",
            name="uq_integration_participants_org_participant_id",
        ),
        tenant_foreign_key(
            "integration_participants",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "integration_participants",
            "crm_user_id",
            "crm_users",
            "user_id",
        ),
        tenant_foreign_key(
            "integration_participants",
            "crm_contact_id",
            "crm_contacts",
            "contact_id",
        ),
        UniqueConstraint(
            "organisation_id",
            "canonical_email_normalized",
            name="uq_integration_participant_org_email",
        ),
        Index("ix_integration_participant_email", "canonical_email_normalized"),
    )

    participant_id: UUID = Field(default_factory=uuid4, primary_key=True)
    canonical_email: str | None = Field(
        default=None,
        sa_column=Column(String(320), nullable=True),
        description="Primary email address selected for the participant.",
    )
    canonical_email_normalized: str | None = Field(
        default=None,
        sa_column=Column(String(320), nullable=True),
        description="Lower-cased email used for identity matching.",
    )
    display_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Preferred display name.",
    )
    given_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Participant first name if available.",
    )
    family_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Participant last name if available.",
    )
    avatar_url: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Provider supplied avatar image URL.",
    )
    crm_user_id: UUID | None = Field(
        default=None,
        index=True,
        description="Resolved CRM user identifier when the participant is internal.",
    )
    crm_contact_id: UUID | None = Field(
        default=None,
        index=True,
        description="Resolved CRM contact identifier for external participants.",
    )
    resolution_status: str = Field(
        default="unresolved",
        sa_column=Column(String(32), nullable=False, server_default=text("'unresolved'")),
        description="Matching status (unresolved, auto_matched, manual).",
    )
    resolution_method: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Most recent matching strategy applied.",
    )
    resolution_confidence: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 4), nullable=True),
        description="Confidence score for the current resolution.",
    )
    last_resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the participant was last resolved to CRM entities.",
    )
    participant_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Aggregated metadata specific to the participant regardless of provider.",
    )


__all__ = [
    "IntegrationParticipant",
]
