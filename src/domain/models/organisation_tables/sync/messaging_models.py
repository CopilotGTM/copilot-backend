"""Messaging integration models for Slack, Teams, Google Chat, etc."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from ..base_mixins import utcnow
from .base import SyncedRecordMixin
from .integration_models import EMPTY_JSONB_SQL

EMPTY_JSONB_ARRAY = "'[]'::jsonb"


class ChannelType(str, Enum):
    """
    Type of messaging channel - provider-agnostic.

    Supports Teams, Slack, Google Chat, Discord, etc.
    Use channel_metadata to store provider-specific details.
    """

    # Workspace/Team-based channels (public within workspace)
    # Teams: Standard channels, Slack: Public channels, Google Chat: Spaces
    PUBLIC = "public"

    # Private channels (restricted membership within workspace)
    # Teams: Private channels, Slack: Private channels, Google Chat: Private spaces
    PRIVATE = "private"

    # 1:1 direct messages
    # Teams: 1:1 chats, Slack: DMs, Google Chat: DMs
    DIRECT_MESSAGE = "direct_message"

    # Ad-hoc group chats (not tied to a workspace/team structure)
    # Teams: Group chats, Slack: Group DMs, Google Chat: Group messages
    GROUP_MESSAGE = "group_message"

    # Thread-based conversations (replies within channels)
    # All platforms: Thread replies
    THREAD = "thread"

    # Meeting-associated chats
    # Teams: Meeting chats, Google Meet: Meeting chats
    MEETING = "meeting"


class MessageType(str, Enum):
    """Type of message content."""

    TEXT = "text"
    FILE = "file"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    SYSTEM_EVENT = "system_event"
    CALL = "call"
    POLL = "poll"


class MessagingChannel(SyncedRecordMixin, table=True):
    """Represents a messaging channel/space from Slack, Teams, Google Chat, etc."""

    __tablename__ = "messaging_channels"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "external_channel_id",
            name="uq_messaging_channel_external",
        ),
        Index("ix_messaging_channel_org_active", "organisation_id", "is_monitored"),
        Index("ix_messaging_channel_name", "channel_name"),
    )

    channel_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_channel_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider's channel/conversation ID (e.g., Slack channel ID).",
    )

    channel_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Human-readable channel name.",
    )
    channel_type: ChannelType = Field(
        default=ChannelType.PUBLIC,
        sa_column=Column(String(32), nullable=False, server_default=text("'public'")),
        description="Type of channel (public/private/dm/group).",
    )
    channel_description: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Channel topic or description.",
    )

    is_monitored: bool = Field(
        default=True,
        sa_column=Column(String, nullable=False, server_default=text("true"), index=True),
        description="Whether to actively sync messages from this channel.",
    )
    sync_from_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Start date for message sync (don't pull entire history).",
    )
    message_retention_days: int = Field(
        default=90,
        description="How many days to retain raw messages before archival (summaries kept forever).",
    )

    participant_count: int = Field(
        default=0,
        description="Number of members in the channel.",
    )
    last_activity_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of the most recent message.",
    )

    is_archived: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether the channel is archived.",
    )
    creator_participant_id: UUID | None = Field(
        default=None,
        foreign_key="integration_participants.participant_id",
        index=True,
        description="Channel creator resolved to canonical participant.",
    )

    channel_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description=(
            "Provider-specific channel metadata. "
            "Should include 'provider' key (teams/slack/google_chat). "
            "Teams: team_id, team_name, membership_type, chat_type. "
            "Slack: workspace_id, is_im, is_mpim. "
            "Google Chat: space_type, space_threading_state."
        ),
    )

    # Application-level sync state (Nango handles OAuth tokens)
    last_sync_cursor: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Application cursor for where we left off (e.g., Slack message ts, last message ID).",
    )
    last_synced_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last successful sync completion timestamp.",
    )
    sync_retry_count: int = Field(
        default=0,
        description="Number of consecutive sync failures for this channel.",
    )

    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Last time this channel metadata was synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent observation during sync.",
    )

    messages: list["MessagingMessage"] = Relationship(back_populates="channel")
    thread_summaries: list["MessagingThreadSummary"] = Relationship(back_populates="channel")


class MessagingChannelMember(SyncedRecordMixin, table=True):
    """Tracks channel membership for compliance and analytics."""

    __tablename__ = "messaging_channel_members"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "participant_id",
            "joined_at",
            name="uq_channel_member",
        ),
        Index("ix_channel_member_channel", "channel_id"),
        Index("ix_channel_member_participant", "participant_id"),
        Index("ix_channel_member_active", "channel_id", "left_at"),
    )

    membership_id: UUID = Field(default_factory=uuid4, primary_key=True)
    channel_id: UUID = Field(
        foreign_key="messaging_channels.channel_id",
        index=True,
        description="Channel this membership belongs to.",
    )
    participant_id: UUID = Field(
        foreign_key="integration_participants.participant_id",
        index=True,
        description="Participant who is/was a member.",
    )

    joined_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the participant joined the channel.",
    )
    left_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the participant left (NULL if still active).",
    )

    member_role: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Role in channel (owner, admin, member, guest).",
    )
    added_by_participant_id: UUID | None = Field(
        default=None,
        foreign_key="integration_participants.participant_id",
        description="Participant who added this member.",
    )

    member_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Provider-specific membership metadata.",
    )


class MessagingDealLink(SyncedRecordMixin, table=True):
    """Tracks message/channel-to-deal associations with detection metadata."""

    __tablename__ = "messaging_deal_links"
    __table_args__ = (
        Index("ix_messaging_deal_link_message", "message_id"),
        Index("ix_messaging_deal_link_channel", "channel_id"),
        Index("ix_messaging_deal_link_deal", "deal_id"),
        Index("ix_messaging_deal_link_method", "detection_method"),
    )

    link_id: UUID = Field(default_factory=uuid4, primary_key=True)
    message_id: UUID | None = Field(
        default=None,
        foreign_key="messaging_messages.message_id",
        index=True,
        description="Message being linked (NULL if channel-level link).",
    )
    channel_id: UUID | None = Field(
        default=None,
        foreign_key="messaging_channels.channel_id",
        index=True,
        description="Channel being linked (NULL if message-level link).",
    )
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Deal being linked to.",
    )

    detection_method: str = Field(
        sa_column=Column(String(64), nullable=False),
        description="How link was detected (channel_config, participant_match, channel_name, manual).",
    )
    confidence_score: float = Field(
        default=1.0,
        description="Confidence score for auto-detected links (0.0-1.0, 1.0 for manual).",
    )

    linked_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the link was created.",
    )
    linked_by: UUID | None = Field(
        default=None,
        description="User who created manual link (NULL for auto-detected).",
    )

    is_verified: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether a user has verified this auto-detected link.",
    )
    verified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When the link was verified.",
    )
    verified_by: UUID | None = Field(
        default=None,
        description="User who verified the link.",
    )

    link_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional context (matched participants, detection details).",
    )


class MessagingMessage(SyncedRecordMixin, table=True):
    """Individual message from a messaging platform."""

    __tablename__ = "messaging_messages"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "external_message_id",
            name="uq_messaging_message_external",
        ),
        Index("ix_messaging_message_channel_sent", "channel_id", "sent_at"),
        Index("ix_messaging_message_thread", "thread_id"),
        Index("ix_messaging_message_participant", "participant_id"),
    )

    message_id: UUID = Field(default_factory=uuid4, primary_key=True)
    channel_id: UUID = Field(
        foreign_key="messaging_channels.channel_id",
        index=True,
        description="Channel this message belongs to.",
    )
    external_message_id: str = Field(
        sa_column=Column(String(255), nullable=False),
        description="Provider's message ID.",
    )

    participant_id: UUID | None = Field(
        default=None,
        foreign_key="integration_participants.participant_id",
        index=True,
        description="Author of the message.",
    )
    external_user_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider's user ID when participant not yet resolved.",
    )

    thread_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True, index=True),
        description="Thread identifier for threaded conversations.",
    )
    parent_message_id: UUID | None = Field(
        default=None,
        foreign_key="messaging_messages.message_id",
        index=True,
        description="Parent message for replies.",
    )

    message_type: MessageType = Field(
        default=MessageType.TEXT,
        sa_column=Column(String(32), nullable=False, server_default=text("'text'")),
        description="Type of message content.",
    )
    content: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Text content of the message.",
    )
    formatted_content: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Formatted/rich text version (markdown, HTML, etc.).",
    )

    sent_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the message was sent (provider timestamp).",
    )
    edited_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last edit timestamp if message was edited.",
    )

    reactions: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Emoji reactions and their counts.",
    )
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="File attachments metadata.",
    )
    mentions: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of mentioned user IDs.",
    )

    is_deleted: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether the message was deleted.",
    )
    is_bot_message: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false")),
        description="Whether the message was sent by a bot.",
    )
    is_important: bool = Field(
        default=False,
        sa_column=Column(String, nullable=False, server_default=text("false"), index=True),
        description="Flag for important messages (starred, pinned, or AI-detected priority).",
    )

    message_embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Optional embedding for granular search (only for important messages).",
    )

    message_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Provider-specific message metadata.",
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

    channel: MessagingChannel | None = Relationship(back_populates="messages")


class MessagingThreadSummary(SyncedRecordMixin, table=True):
    """AI-generated summaries of message threads for context retrieval."""

    __tablename__ = "messaging_thread_summaries"
    __table_args__ = (
        Index("ix_messaging_summary_channel_time", "channel_id", "time_range_start"),
        Index("ix_messaging_summary_deal", "deal_id"),
        Index("ix_messaging_summary_thread", "thread_id"),
    )

    summary_id: UUID = Field(default_factory=uuid4, primary_key=True)
    channel_id: UUID = Field(
        foreign_key="messaging_channels.channel_id",
        index=True,
        description="Channel the summary is for.",
    )

    thread_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Thread identifier if this is a thread-level summary.",
    )
    deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Associated deal for deal-linked channels.",
    )

    summary_text: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Concise AI-generated summary of the conversation.",
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
        description="Participant stats (message counts, key contributors).",
    )

    message_count: int = Field(
        default=0,
        description="Number of messages included in this summary.",
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

    source_message_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY)),
        description="List of message IDs included in this summary.",
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

    summary_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional metadata.",
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

    channel: MessagingChannel | None = Relationship(back_populates="thread_summaries")


__all__ = [
    "ChannelType",
    "MessageType",
    "MessagingChannel",
    "MessagingChannelMember",
    "MessagingDealLink",
    "MessagingMessage",
    "MessagingThreadSummary",
]
