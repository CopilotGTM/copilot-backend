from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from ..base_mixins import utcnow
from .base import SyncedRecordMixin
from .integration_models import EMPTY_JSONB_SQL

if TYPE_CHECKING:
    from .conversation_intelligence import CICallSpeaker


class IntegrationParticipant(SyncedRecordMixin, table=True):
    """Canonical participant record across integrations."""

    __tablename__ = "integration_participants"
    __table_args__ = (
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
        foreign_key="crm_users.user_id",
        index=True,
        description="Resolved CRM user identifier when the participant is internal.",
    )
    crm_contact_id: UUID | None = Field(
        default=None,
        foreign_key="crm_contacts.contact_id",
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

    identities: list["IntegrationParticipantIdentity"] = Relationship(back_populates="participant")
    speakers: list["CICallSpeaker"] = Relationship(back_populates="participant")


class IntegrationParticipantIdentity(SyncedRecordMixin, table=True):
    """Provider level identity mapping tied to the canonical participant."""

    __tablename__ = "integration_participant_identities"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "provider_participant_id",
            name="uq_integration_participant_identity_provider",
        ),
        Index("ix_integration_part_identity_email", "provider_email_normalized"),
    )

    identity_id: UUID = Field(default_factory=uuid4, primary_key=True)
    participant_id: UUID = Field(
        foreign_key="integration_participants.participant_id",
        index=True,
        description="Canonical participant this provider identity resolves to.",
    )
    provider_participant_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider specific participant identifier (e.g. Gong user id).",
    )
    provider_email: str | None = Field(
        default=None,
        sa_column=Column(String(320), nullable=True),
        description="Email observed from the provider for this identity.",
    )
    provider_email_normalized: str | None = Field(
        default=None,
        sa_column=Column(String(320), nullable=True),
        description="Normalized (lowercase) email for matching.",
    )
    provider_display_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name returned by the provider for the identity.",
    )
    first_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this identity was first observed.",
    )
    last_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Most recent observation timestamp for this identity.",
    )
    match_confidence: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 4), nullable=True),
        description="Confidence assigned when linking the identity to the canonical participant.",
    )
    identity_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional provider specific metadata for the identity.",
    )

    participant: IntegrationParticipant | None = Relationship(back_populates="identities")


__all__ = [
    "IntegrationParticipant",
    "IntegrationParticipantIdentity",
]
