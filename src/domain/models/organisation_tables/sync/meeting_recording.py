"""Provider-agnostic meeting recording session tracking."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ..base_mixins import TimestampMixin
from ..tenant_constraints import tenant_foreign_key
from .integration_models import EMPTY_JSONB_SQL


class MeetingRecordingProvider(str, Enum):
    """Supported meeting recording backends."""

    RECALL_AI = "recall_ai"


class MeetingRecordingStatus(str, Enum):
    """Normalized orchestration lifecycle for a recording session."""

    PENDING_SEND = "PENDING_SEND"
    SEND_QUEUED = "SEND_QUEUED"
    SENT = "SENT"
    JOINING = "JOINING"
    IN_CALL = "IN_CALL"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class MeetingTranscriptStatus(str, Enum):
    """Transcript lifecycle independent of provider transport state."""

    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class MeetingRecordingSession(TimestampMixin, SQLModel, table=True):
    """Tracks one org-scoped provider recording attempt for a calendar event.

    Matching and dispatch create at most one row per org/event/provider. Later
    webhook events update this same row so provider status and transcript state
    stay correlated without cross-org lookups.
    """

    __tablename__ = "meeting_recording_sessions"
    __table_args__ = (
        tenant_foreign_key(
            "meeting_recording_sessions",
            "calendar_event_id",
            "calendar_events",
            "event_id",
        ),
        # The matcher may see the same event from multiple passes, so the DB
        # constraint is the final idempotency guard for session creation.
        UniqueConstraint(
            "organisation_id",
            "calendar_event_id",
            "provider",
            name="uq_meeting_recording_org_event_provider",
        ),
        UniqueConstraint(
            "organisation_id",
            "provider_session_id",
            name="uq_meeting_recording_provider_session_org",
        ),
        UniqueConstraint(
            "organisation_id",
            "dedupe_key",
            name="uq_meeting_recording_dedupe_key_org",
        ),
        Index("ix_meeting_recording_org_status", "organisation_id", "status"),
        Index("ix_meeting_recording_org_join_at", "organisation_id", "join_at"),
        Index(
            "ix_meeting_recording_unsent_lookup",
            "organisation_id",
            "sent_to_provider_at",
            "join_at",
        ),
    )

    session_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    calendar_event_id: UUID = Field(
        index=True,
        description="Calendar event this provider recording session belongs to.",
    )
    provider: MeetingRecordingProvider = Field(
        default=MeetingRecordingProvider.RECALL_AI,
        sa_column=Column(String(32), nullable=False, server_default=text("'recall_ai'")),
    )
    dedupe_key: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Stable org-scoped idempotency key for provider session creation.",
    )
    meeting_url: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Join URL sent to the provider.",
    )
    join_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Planned time when the provider bot should join.",
    )
    status: MeetingRecordingStatus = Field(
        default=MeetingRecordingStatus.PENDING_SEND,
        sa_column=Column(String(32), nullable=False, server_default=text("'PENDING_SEND'")),
    )
    transcript_status: MeetingTranscriptStatus = Field(
        default=MeetingTranscriptStatus.NOT_REQUESTED,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'NOT_REQUESTED'"),
        ),
    )
    provider_session_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider bot/session identifier once the create call succeeds.",
    )
    provider_status: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Latest raw provider lifecycle state.",
    )
    sent_to_provider_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Set only after the provider accepts create session.",
    )
    transcript_ready_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the provider first reported a downloadable transcript.",
    )
    video_blob_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable org-blob path for the archived mixed MP4.",
    )
    video_blob_url: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Stable non-SAS blob URL for the archived mixed MP4.",
    )
    video_content_type: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Content type stored with the archived mixed MP4.",
    )
    video_size_bytes: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
        description="Archived mixed MP4 size when known.",
    )
    video_recording_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider recording id for the archived mixed MP4.",
    )
    video_artifact_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider video artifact id for the archived mixed MP4.",
    )
    video_archived_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the mixed MP4 was archived into org storage.",
    )
    last_webhook_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last provider callback processed for this session.",
    )
    status_message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Latest provider message/sub-code worth surfacing for debugging.",
    )
    retry_count: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    provider_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Merged raw provider responses and webhook snapshots for this session.",
    )


__all__ = [
    "MeetingRecordingProvider",
    "MeetingRecordingStatus",
    "MeetingTranscriptStatus",
    "MeetingRecordingSession",
]
