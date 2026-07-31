"""
Conversation Intelligence Models (DEPRECATED).

⚠️  DEPRECATED: Use meeting_intelligence.py for new development.

This module was designed for syncing data FROM external CI tools (Gong, Chorus)
where we don't own the data - we just mirror it. It has sync overhead fields
like synced_at, last_seen_at, source_modified_at.

For native meeting intelligence (Teams transcripts, Zoom, our own bot), use:
- Meeting (meeting_intelligence.py)
- TranscriptSegment (meeting_intelligence.py)
- CalendarEventDealAssociation.meeting_summary_data for insights

This module is kept for:
1. Backward compatibility with existing data
2. Future Gong/Chorus integration if needed (external sync use case)
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from ..base_mixins import utcnow
from ..tenant_constraints import tenant_foreign_key
from .base import SyncedRecordMixin
from .integration_models import EMPTY_JSONB_SQL
from .participant_models import IntegrationParticipant, IntegrationParticipantIdentity

# Constants for repeated string literals
_DESC_SOURCE_MODIFIED = "Last modified timestamp reported by the provider."
_FK_CI_CALLS_CALL_ID = "ci_calls.call_id"
_DESC_CI_CALL_ID = "Conversation intelligence call identifier."
_EMPTY_JSONB_ARRAY = "'[]'::jsonb"


class CICallMedium(str, Enum):
    """Medium used for the captured conversation."""

    MEETING = "meeting"
    PHONE = "phone"
    VIDEO = "video"
    SCREEN_SHARE = "screen_share"
    OTHER = "other"


class CICallRecordingStatus(str, Enum):
    """Lifecycle state for the synced recording/transcript."""

    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class CISpeakerRole(str, Enum):
    """Classification for the speaker profile."""

    INTERNAL = "internal"
    CUSTOMER = "customer"
    PARTNER = "partner"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class TranscriptContentFormat(str, Enum):
    """Format used for stored transcript content."""

    TEXT = "text"
    RICH_TEXT = "rich_text"
    JSON = "json"


class CICall(SyncedRecordMixin, table=True):
    """Conversation intelligence call metadata sourced from Gong/Chorus/etc."""

    __tablename__ = "ci_calls"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "call_id",
            name="uq_ci_calls_org_call_id",
        ),
        tenant_foreign_key(
            "ci_calls",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key("ci_calls", "crm_owner_id", "crm_users", "user_id"),
        tenant_foreign_key(
            "ci_calls",
            "primary_deal_id",
            "crm_deals",
            "deal_id",
        ),
        UniqueConstraint(
            "organisation_id",
            "integration_id",
            "external_id",
            name="uq_ci_call_external_org",
        ),
        Index("ix_ci_call_org_started", "organisation_id", "started_at"),
        Index("ix_ci_call_org_primary_deal", "organisation_id", "primary_deal_id"),
    )

    call_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))

    title: str | None = Field(default=None, description="Provider-supplied call title.")
    description: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Optional description or agenda text.",
    )
    call_medium: CICallMedium = Field(
        default=CICallMedium.MEETING,
        sa_column=Column(String(32), nullable=False, server_default=text("'meeting'")),
        description="Medium of the captured conversation.",
    )
    recording_status: CICallRecordingStatus = Field(
        default=CICallRecordingStatus.PROCESSING,
        sa_column=Column(String(32), nullable=False, server_default=text("'processing'")),
        description="Processing lifecycle for the transcript/recording.",
    )

    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Conversation start timestamp.",
    )
    ended_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Conversation end timestamp.",
    )
    duration_seconds: int | None = Field(
        default=None, description="Duration in seconds reported by the provider."
    )
    timezone: str | None = Field(default=None, description="Provider timezone string.")
    meeting_url: str | None = Field(
        default=None, description="Original meeting invite URL, when provided."
    )
    recording_url: str | None = Field(
        default=None,
        description="Direct URL to retrieve the call recording blob.",
    )
    transcript_language: str | None = Field(
        default=None,
        description="BCP-47 language tag detected for the transcript.",
    )

    owner_user_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Provider identifier for the internal owner of the call.",
    )
    crm_owner_id: UUID | None = Field(
        default=None,
        index=True,
        description="Resolved CRM user owning the call.",
    )
    primary_deal_id: UUID | None = Field(
        default=None,
        index=True,
        description="Primary CRM deal the provider associates with the call.",
    )
    primary_deal_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider deal ID used for the main association.",
    )

    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Original creation timestamp reported by the provider.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description=_DESC_SOURCE_MODIFIED,
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this call row was last synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this call was observed during sync.",
    )

    call_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Flattened metadata surfaced by the CI provider.",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider payload for debugging.",
    )

    transcript_segments: list["CICallTranscriptSegment"] = Relationship(back_populates="call")
    analytics_snapshots: list["CICallAnalyticsSnapshot"] = Relationship(back_populates="call")
    deal_links: list["CICallDealLink"] = Relationship(back_populates="call")
    speakers: list["CICallSpeaker"] = Relationship(
        back_populates="call",
        sa_relationship_kwargs={"overlaps": "speakers"},
    )


class CICallDealLink(SyncedRecordMixin, table=True):
    """Many-to-many association between CI calls and CRM deals."""

    __tablename__ = "ci_call_deal_links"
    __table_args__ = (
        tenant_foreign_key(
            "ci_call_deal_links",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "ci_call_deal_links",
            "call_id",
            "ci_calls",
            "call_id",
        ),
        tenant_foreign_key(
            "ci_call_deal_links",
            "deal_id",
            "crm_deals",
            "deal_id",
        ),
        UniqueConstraint(
            "organisation_id",
            "integration_id",
            "call_id",
            "deal_id",
            name="uq_ci_call_deal_org",
        ),
        UniqueConstraint(
            "organisation_id",
            "integration_id",
            "call_id",
            "deal_external_id",
            name="uq_ci_call_deal_external_org",
        ),
        Index("ix_ci_call_deal_deal_id", "deal_id"),
        Index("ix_ci_call_deal_external", "deal_external_id"),
    )

    link_id: UUID = Field(default_factory=uuid4, primary_key=True)
    call_id: UUID = Field(
        index=True,
        description=_DESC_CI_CALL_ID,
    )
    deal_id: UUID = Field(
        index=True,
        description="CRM deal associated with the call.",
    )
    deal_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider deal identifier used before the CRM deal row is materialised.",
    )

    association_type: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Provider hint on the relationship, e.g. primary/mentioned.",
    )
    confidence_score: float | None = Field(
        default=None,
        description="Optional provider confidence score for the linkage.",
    )
    linked_reason: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description="Free-form note justifying the association.",
    )

    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this link row was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description=_DESC_SOURCE_MODIFIED,
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this link was observed during sync.",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Provider-specific metadata for the association.",
    )

    call: CICall | None = Relationship(back_populates="deal_links")


class CICallSpeaker(SyncedRecordMixin, table=True):
    """Participant metadata for a CI call."""

    __tablename__ = "ci_call_speakers"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "speaker_id",
            name="uq_ci_call_speakers_org_speaker_id",
        ),
        tenant_foreign_key(
            "ci_call_speakers",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "ci_call_speakers",
            "call_id",
            "ci_calls",
            "call_id",
        ),
        tenant_foreign_key(
            "ci_call_speakers",
            "participant_id",
            "integration_participants",
            "participant_id",
        ),
        tenant_foreign_key(
            "ci_call_speakers",
            "crm_contact_id",
            "crm_contacts",
            "contact_id",
        ),
        tenant_foreign_key(
            "ci_call_speakers",
            "crm_user_id",
            "crm_users",
            "user_id",
        ),
        UniqueConstraint(
            "organisation_id",
            "integration_id",
            "call_id",
            "external_participant_id",
            name="uq_ci_call_speaker_external_org",
        ),
        Index("ix_ci_call_speaker_call_role", "call_id", "speaker_role"),
    )

    speaker_id: UUID = Field(default_factory=uuid4, primary_key=True)
    call_id: UUID = Field(
        index=True,
        description=_DESC_CI_CALL_ID,
    )
    external_participant_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider identifier for the participant profile.",
    )

    speaker_role: CISpeakerRole = Field(
        default=CISpeakerRole.UNKNOWN,
        sa_column=Column(String(32), nullable=False, server_default=text("'unknown'")),
        description="Role classification for the participant.",
    )
    speaker_name: str | None = Field(default=None, description="Display name for speaker.")
    speaker_email: str | None = Field(
        default=None,
        sa_column=Column(String(320), nullable=True),
        description="Email address associated with the speaker.",
    )
    participant_id: UUID | None = Field(
        default=None,
        index=True,
        description="Canonical participant identity linked to this speaker instance.",
    )
    crm_contact_id: UUID | None = Field(
        default=None,
        index=True,
        description="Resolved CRM contact identifier for external speakers.",
    )
    crm_user_id: UUID | None = Field(
        default=None,
        index=True,
        description="Resolved CRM user identifier for internal speakers.",
    )

    talk_time_seconds: int | None = Field(
        default=None, description="Total seconds spoken by this participant."
    )
    talk_ratio: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 2), nullable=True),
        description="Percentage of total conversation time attributed to this speaker.",
    )
    sentiment_score: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 2), nullable=True),
        description="Average sentiment score for the speaker, when provided.",
    )
    speaker_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Provider-specific participant properties.",
    )

    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this speaker row was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description=_DESC_SOURCE_MODIFIED,
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this speaker was observed during sync.",
    )

    call: CICall | None = Relationship(
        back_populates="speakers",
        sa_relationship_kwargs={"overlaps": "speakers"},
    )
    transcript_segments: list["CICallTranscriptSegment"] = Relationship(
        back_populates="speaker",
        sa_relationship_kwargs={"overlaps": "transcript_segments"},
    )
    participant: IntegrationParticipant | None = Relationship(
        back_populates="speakers",
        sa_relationship_kwargs={"overlaps": "call,speakers"},
    )


class CICallTranscriptSegment(SyncedRecordMixin, table=True):
    """Normalized transcript text broken into provider-defined segments."""

    __tablename__ = "ci_transcript_segments"
    __table_args__ = (
        tenant_foreign_key(
            "ci_transcript_segments",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "ci_transcript_segments",
            "call_id",
            "ci_calls",
            "call_id",
        ),
        tenant_foreign_key(
            "ci_transcript_segments",
            "speaker_id",
            "ci_call_speakers",
            "speaker_id",
        ),
        UniqueConstraint(
            "organisation_id",
            "integration_id",
            "call_id",
            "segment_index",
            name="uq_ci_transcript_sequence_org",
        ),
        Index("ix_ci_transcript_call_sequence", "call_id", "segment_index"),
    )

    segment_id: UUID = Field(default_factory=uuid4, primary_key=True)
    call_id: UUID = Field(
        index=True,
        description=_DESC_CI_CALL_ID,
    )
    speaker_id: UUID | None = Field(
        default=None,
        index=True,
        description="Speaker delivering this portion of the transcript.",
    )
    speaker_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider participant identifier when the speaker row is not yet materialised.",
    )

    segment_index: int = Field(
        default=0,
        description="0-based ordering value to rebuild the transcript sequentially.",
    )
    start_time_seconds: float | None = Field(
        default=None,
        description="Start time offset (seconds) from the beginning of the recording.",
    )
    end_time_seconds: float | None = Field(
        default=None,
        description="End time offset (seconds) from the beginning of the recording.",
    )
    content: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Plain text transcript content for the segment.",
    )
    content_format: TranscriptContentFormat = Field(
        default=TranscriptContentFormat.TEXT,
        sa_column=Column(String(32), nullable=False, server_default=text("'text'")),
        description="Format of transcript payload (text, rich_text, json).",
    )
    language_code: str | None = Field(
        default=None,
        description="Language detected for this portion of the transcript.",
    )
    sentiment_score: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 2), nullable=True),
        description="Sentiment score for the segment, when supplied.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(_EMPTY_JSONB_ARRAY),
        ),
        description="Keyword or tracker matches surfaced for the segment.",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Raw provider payload for the segment.",
    )

    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this transcript segment was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description=_DESC_SOURCE_MODIFIED,
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this segment was observed during sync.",
    )

    call: CICall | None = Relationship(
        back_populates="transcript_segments",
        sa_relationship_kwargs={"overlaps": "transcript_segments"},
    )
    speaker: CICallSpeaker | None = Relationship(
        back_populates="transcript_segments",
        sa_relationship_kwargs={"overlaps": "call,transcript_segments"},
    )


class CICallAnalyticsSnapshot(SyncedRecordMixin, table=True):
    """Analytics payloads (summaries, trackers, sentiment) tied to a CI call."""

    __tablename__ = "ci_call_analytics"
    __table_args__ = (
        tenant_foreign_key(
            "ci_call_analytics",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "ci_call_analytics",
            "call_id",
            "ci_calls",
            "call_id",
        ),
        UniqueConstraint(
            "organisation_id",
            "integration_id",
            "call_id",
            "analysis_type",
            name="uq_ci_call_analysis_type_org",
        ),
        Index("ix_ci_call_analytics_timeline", "call_id", "generated_at"),
    )

    analytics_id: UUID = Field(default_factory=uuid4, primary_key=True)
    call_id: UUID = Field(
        index=True,
        description=_DESC_CI_CALL_ID,
    )
    analysis_type: str = Field(
        default="summary",
        sa_column=Column(
            String(64),
            nullable=False,
            server_default=text("'summary'"),
        ),
        description="Type of analytics payload (summary, trackers, sentiment, etc.).",
    )
    generated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when this analytics snapshot was produced.",
    )

    summary: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description="Provider-generated narrative summary.",
    )
    highlights: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(_EMPTY_JSONB_ARRAY),
        ),
        description="Key highlights or snippets.",
    )
    action_items: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(_EMPTY_JSONB_ARRAY),
        ),
        description="Action items extracted from the conversation.",
    )
    topics: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(_EMPTY_JSONB_ARRAY),
        ),
        description="Tracked topics, keywords, or objections.",
    )
    overall_sentiment: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 2), nullable=True),
        description="Overall sentiment score returned by the provider.",
    )
    talk_time_ratio: float | None = Field(
        default=None,
        sa_column=Column(Numeric(5, 2), nullable=True),
        description="Percentage of time internal parties spoke vs. customers.",
    )
    analytics_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Structured analytics metrics and trackers.",
    )

    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this analytics snapshot was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description=_DESC_SOURCE_MODIFIED,
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this snapshot was observed during sync.",
    )

    call: CICall | None = Relationship(back_populates="analytics_snapshots")


__all__ = [
    "IntegrationParticipant",
    "IntegrationParticipantIdentity",
    "CICall",
    "CICallDealLink",
    "CICallSpeaker",
    "CICallTranscriptSegment",
    "CICallAnalyticsSnapshot",
    "CICallMedium",
    "CICallRecordingStatus",
    "CISpeakerRole",
    "TranscriptContentFormat",
]
