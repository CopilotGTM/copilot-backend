"""Email integration models for Gmail, Outlook, etc."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from ..base_mixins import utcnow
from .base import SyncedRecordMixin
from .calendar import AssociationStatus
from .integration_models import EMPTY_JSONB_SQL

EMPTY_JSONB_ARRAY = "'[]'::jsonb"


class EmailDirection(str, Enum):
    """Direction of email flow."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class DealEmailDirection(str, Enum):
    """Direction of email relative to the deal team."""

    INBOUND = "INBOUND"  # From external contact to team
    OUTBOUND = "OUTBOUND"  # From team to external contact
    INTERNAL = "INTERNAL"  # Between team members (CC'd contacts)


class EmailPriority(str, Enum):
    """Email priority level."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class EmailFolder(SyncedRecordMixin, table=True):
    """Represents an email folder/label from Gmail, Outlook, etc."""

    __tablename__ = "email_folders"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "external_folder_id",
            name="uq_email_folder_external",
        ),
        Index("ix_email_folder_org_monitored", "organisation_id", "is_monitored"),
        Index("ix_email_folder_name", "folder_name"),
    )

    folder_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_folder_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider's folder/label ID (e.g., Gmail label ID, Outlook folder ID).",
    )

    folder_name: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Folder/label name (e.g., 'Inbox', 'Deals', 'Customer Support').",
    )
    folder_path: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Full folder path for nested folders (e.g., 'Inbox/Deals/Acme').",
    )
    folder_type: str = Field(
        default="user",
        sa_column=Column(String(32), nullable=False, server_default=text("'user'")),
        description="Type of folder (inbox/sent/drafts/trash/user/archive).",
    )

    is_monitored: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false"), index=True),
        description="Whether to actively sync emails from this folder.",
    )
    sync_from_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Start date for email sync (don't pull entire history).",
    )
    email_retention_days: int = Field(
        default=180,
        description="How many days to retain raw emails before archival (summaries kept forever).",
    )

    total_count: int = Field(
        default=0,
        description="Total number of emails in folder.",
    )
    unread_count: int = Field(
        default=0,
        description="Number of unread emails.",
    )
    last_activity_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of the most recent email.",
    )

    folder_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Provider-specific folder metadata.",
    )

    # Application-level sync state (Nango handles OAuth tokens and provider-specific cursors)
    last_sync_cursor: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Application cursor for where we left off (e.g., last email ID, message timestamp).",
    )
    last_synced_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last successful sync completion timestamp.",
    )
    sync_retry_count: int = Field(
        default=0,
        description="Number of consecutive sync failures for this folder.",
    )

    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Last time this folder metadata was synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent observation during sync.",
    )

    emails: list["Email"] = Relationship(back_populates="folder")
    thread_summaries: list["EmailThreadSummary"] = Relationship(back_populates="folder")


class Email(SyncedRecordMixin, table=True):
    """Individual email message from email provider."""

    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "external_message_id",
            name="uq_email_external",
        ),
        Index("ix_email_folder_received", "folder_id", "received_at"),
        Index("ix_email_thread", "thread_id"),
        Index("ix_email_internet_message_id", "organisation_id", "internet_message_id"),
        Index("ix_email_from_participant", "from_participant_id"),
        Index("ix_email_deal", "deal_id"),
    )

    email_id: UUID = Field(default_factory=uuid4, primary_key=True)
    folder_id: UUID = Field(
        foreign_key="email_folders.folder_id",
        index=True,
        description="Folder this email belongs to.",
    )
    external_message_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider's message ID.",
    )

    thread_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True, index=True),
        description="Email thread identifier for conversation grouping.",
    )
    internet_message_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="RFC 822 Message-ID header. Same across all recipients - use for cross-user deduplication and deal mapping.",
    )
    in_reply_to: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Message ID this email is replying to.",
    )
    references: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of message IDs in the conversation chain.",
    )

    from_participant_id: UUID | None = Field(
        default=None,
        foreign_key="integration_participants.participant_id",
        index=True,
        description="Sender resolved to canonical participant.",
    )
    from_email: str = Field(
        sa_column=Column(String(320), nullable=False, index=True),
        description="Sender email address.",
    )
    from_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Sender display name.",
    )

    to_emails: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of recipient email addresses.",
    )
    to_participants: list[UUID] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of resolved participant IDs for recipients.",
    )
    cc_emails: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of CC email addresses.",
    )
    bcc_emails: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of BCC email addresses (if accessible).",
    )

    subject: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Email subject line.",
    )
    body_text: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Plain text body content.",
    )
    body_html: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="HTML body content.",
    )
    snippet: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Short preview/snippet of email content.",
    )

    received_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the email was received (provider timestamp).",
    )
    sent_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the email was sent by the sender.",
    )

    direction: EmailDirection = Field(
        default=EmailDirection.INBOUND,
        sa_column=Column(String(32), nullable=False, server_default=text("'inbound'")),
        description="Email direction (inbound/outbound).",
    )
    priority: EmailPriority = Field(
        default=EmailPriority.NORMAL,
        sa_column=Column(String(32), nullable=False, server_default=text("'normal'")),
        description="Email priority level.",
    )

    is_read: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether the email has been read.",
    )
    is_starred: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether the email is starred/flagged.",
    )
    is_important: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false"), index=True),
        description="Whether the email is marked as important (or AI-detected priority).",
    )
    is_draft: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether the email is a draft.",
    )

    has_attachments: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether the email has attachments.",
    )
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="File attachments metadata (name, size, mime_type, download_url).",
    )

    labels: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="Gmail labels or Outlook categories.",
    )

    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Associated deal (auto-detected or manually linked).",
    )

    email_embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Optional embedding for important emails.",
    )

    email_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Provider-specific email metadata (headers, etc.).",
    )

    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Last sync timestamp.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent observation during sync.",
    )

    folder: EmailFolder | None = Relationship(back_populates="emails")


class EmailThreadSummary(SyncedRecordMixin, table=True):
    """AI-generated summaries of email threads for context retrieval."""

    __tablename__ = "email_thread_summaries"
    __table_args__ = (
        Index("ix_email_summary_folder_time", "folder_id", "time_range_start"),
        Index("ix_email_summary_deal", "deal_id"),
        Index("ix_email_summary_thread", "thread_id"),
    )

    summary_id: UUID = Field(default_factory=uuid4, primary_key=True)
    folder_id: UUID = Field(
        foreign_key="email_folders.folder_id",
        index=True,
        description="Folder the summary is for.",
    )

    thread_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Email thread identifier.",
    )
    subject: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Thread subject line.",
    )

    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Associated deal for deal-linked threads.",
    )

    summary_text: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Concise AI-generated summary of the email thread.",
    )
    key_points: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="Key discussion points extracted.",
    )
    action_items: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="Action items identified.",
    )
    participants_summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Participant stats (email counts, key contributors).",
    )

    email_count: int = Field(
        default=0,
        description="Number of emails included in this summary.",
    )
    time_range_start: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Start of the time range covered.",
    )
    time_range_end: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="End of the time range covered.",
    )

    sentiment_score: float | None = Field(
        default=None,
        description="Overall sentiment score (-1 to 1).",
    )
    urgency_level: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Urgency classification (low/medium/high/critical).",
    )
    topics: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="Identified topics/themes.",
    )

    summary_embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Vector embedding of the summary for semantic search.",
    )

    source_email_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of email IDs included in this summary.",
    )
    generated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the summary was generated.",
    )
    generated_by: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Model/version used for generation.",
    )

    exported_to_kb: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false"), index=True),
        description="Whether this summary has been exported to the knowledge base.",
    )
    kb_source_id: UUID | None = Field(
        default=None,
        index=True,
        description="Reference to the OrganisationSource created from this summary.",
    )

    summary_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional metadata.",
    )

    folder: EmailFolder | None = Relationship(back_populates="thread_summaries")


class DealEmailAssociation(SQLModel, table=True):
    """
    Links emails to deals using internet_message_id for cross-user deduplication.

    Follows the same pattern as CalendarEventDealAssociation. One association per
    unique email per deal - if the same email appears in multiple user mailboxes,
    only one association is created (using internet_message_id).
    """

    __tablename__ = "deal_email_associations"
    __table_args__ = (
        UniqueConstraint("deal_id", "internet_message_id", name="uq_deal_email"),
        Index("ix_deal_email_org_status", "organisation_id", "status"),
        Index("ix_deal_email_deal", "deal_id"),
        Index("ix_deal_email_internet_msg", "internet_message_id"),
    )

    association_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        description="The deal this email is associated with.",
    )
    internet_message_id: str = Field(
        sa_column=Column(String(512), nullable=False),
        description="RFC 822 Message-ID - same across all recipients of the email.",
    )

    # Matching metadata (same pattern as CalendarEventDealAssociation)
    status: AssociationStatus = Field(
        default=AssociationStatus.ACTIVE,
        sa_column=Column(String(32), nullable=False, server_default=text("'ACTIVE'")),
        description="Association status: ACTIVE (visible), REMOVED (user removed), or CANCELLED.",
    )
    match_score: float = Field(
        default=0.0,
        description="Confidence score 0.0-1.0 based on stakeholder matching.",
    )

    # Email-specific metadata
    direction: DealEmailDirection = Field(
        default=DealEmailDirection.INBOUND,
        sa_column=Column(String(32), nullable=False, server_default=text("'INBOUND'")),
        description="Direction relative to deal team: INBOUND, OUTBOUND, or INTERNAL.",
    )
    matched_stakeholder_emails: list[str] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Which stakeholder emails matched this email for audit trail.",
    )

    # Email context (denormalized for quick access without joining)
    email_subject: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Email subject line for display purposes.",
    )
    email_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the email was sent/received.",
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


__all__ = [
    "EmailDirection",
    "DealEmailDirection",
    "EmailPriority",
    "EmailFolder",
    "Email",
    "EmailThreadSummary",
    "DealEmailAssociation",
]
