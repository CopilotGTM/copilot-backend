"""List deal channel messages tool for agents.

Tool for listing messages from messaging channels (Teams, Slack, etc.)
associated with a specific deal. Uses MessagingDealLink to find deal-linked channels.

Design: Always returns a structured response with messages organized by channel,
making it clear which messages came from which channel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.sql.functions import count

from src.agents.tools.base import db_session_lock, execute_with_capabilities
from src.agents.tools.crm.access import AgentCRMAccessError, require_agent_deal_access
from src.domain.models.organisation_tables.sync.messaging_models import (
    MessagingChannel,
    MessagingDealLink,
    MessagingMessage,
)

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


async def list_deal_channel_messages(
    *,
    context: AgentContext,
    deal_id: str | None = None,
    channel_id: str | None = None,
    messages_per_channel: int = 20,
    include_bot_messages: bool = False,
    sender_email: str | None = None,
    sender_name: str | None = None,
) -> dict[str, Any]:
    """List messages from channels associated with a deal.

    Returns a structured response with messages organized by channel, ensuring
    balanced representation across all channels.

    Args:
        context: AgentContext containing session and organisation metadata
        deal_id: Optional deal UUID. If not provided, uses context.deal_id
        channel_id: Optional channel UUID to filter to a specific channel
        messages_per_channel: Maximum messages to return PER channel (default: 20)
        include_bot_messages: Include messages from bots (default: False)
        sender_email: Filter messages sent by this email address (exact, case-insensitive)
        sender_name: Filter messages where sender name contains this string (ILIKE)

    Returns:
        Structured dict with:
        - channels: List of channel objects, each containing channel info and messages
        - total_messages: Total count of messages returned
        - total_channels: Number of channels
    """
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("list_deal_channel_messages: missing organisation_id")
        return {"channels": [], "total_messages": 0, "total_channels": 0}

    # Use context deal_id if not provided
    actual_deal_id = deal_id or (str(context.deal_id) if context.deal_id else None)
    if not actual_deal_id:
        logger.warning("list_deal_channel_messages: no deal_id provided and no context deal_id")
        return {"channels": [], "total_messages": 0, "total_channels": 0}

    # Parse deal_id
    try:
        parsed_deal_id = UUID(actual_deal_id)
    except ValueError:
        logger.warning(f"list_deal_channel_messages: invalid deal_id format: {actual_deal_id}")
        return {"channels": [], "total_messages": 0, "total_channels": 0}

    # Parse optional channel_id filter
    parsed_channel_id: UUID | None = None
    if channel_id:
        try:
            parsed_channel_id = UUID(channel_id)
        except ValueError:
            logger.warning(f"list_deal_channel_messages: invalid channel_id format: {channel_id}")

    # Acquire session on-demand (fresh session pattern)
    async with context.acquire_session() as session:
        try:
            await require_agent_deal_access(context, session, parsed_deal_id)
        except AgentCRMAccessError:
            logger.info("list_deal_channel_messages: deal not visible to agent actor")
            return {"channels": [], "total_messages": 0, "total_channels": 0}
        # Type-safe column access pattern
        link_c = cast(Any, MessagingDealLink).__table__.c
        channel_c = cast(Any, MessagingChannel).__table__.c
        message_c = cast(Any, MessagingMessage).__table__.c

        # Step 1: Find channels linked to this deal with metadata
        channel_stmt = (
            select(
                MessagingChannel,
                count(message_c.message_id).label("message_count"),
            )
            .join(
                MessagingDealLink,
                channel_c.channel_id == link_c.channel_id,
            )
            .outerjoin(
                MessagingMessage,
                channel_c.channel_id == message_c.channel_id,
            )
            .where(
                link_c.organisation_id == organisation_id,
                link_c.deal_id == parsed_deal_id,
                link_c.channel_id.isnot(None),
            )
            .group_by(channel_c.channel_id)
            .order_by(channel_c.last_activity_at.desc().nullslast())
        )

        # Filter to specific channel if provided
        if parsed_channel_id:
            channel_stmt = channel_stmt.where(channel_c.channel_id == parsed_channel_id)

        try:
            channel_result = await session.execute(channel_stmt)
            channel_rows = channel_result.fetchall()
        except Exception as e:
            logger.exception(f"list_deal_channel_messages: error fetching channels: {e}")
            return {"channels": [], "total_messages": 0, "total_channels": 0}

        if not channel_rows:
            logger.info(f"list_deal_channel_messages: no channels linked to deal {actual_deal_id}")
            return {"channels": [], "total_messages": 0, "total_channels": 0}

        # Build channel info and collect channel IDs
        channels_info: dict[UUID, dict[str, Any]] = {}
        for row in channel_rows:
            channel = row[0]
            total_msg_count = row[1] or 0

            # Get provider from metadata
            provider = "unknown"
            if channel.channel_metadata:
                provider = channel.channel_metadata.get("provider", "unknown")

            channels_info[channel.channel_id] = {
                "channel_id": str(channel.channel_id),
                "channel_name": channel.channel_name or "Unnamed Channel",
                "channel_type": channel.channel_type.value
                if hasattr(channel.channel_type, "value")
                else str(channel.channel_type),
                "provider": provider,
                "total_message_count": total_msg_count,
                "last_activity_at": channel.last_activity_at.isoformat()
                if channel.last_activity_at
                else None,
                "messages": [],
            }

        # Step 2: Fetch messages for each channel (with per-channel limit)
        # Using a single query with ROW_NUMBER() would be ideal, but for simplicity
        # we'll fetch messages ordered by channel and time, then distribute
        linked_channel_ids = list(channels_info.keys())

        message_stmt = (
            select(
                MessagingMessage,
                message_c.author_display_name,
                message_c.author_email,
            )
            .where(
                message_c.organisation_id == organisation_id,
                message_c.channel_id.in_(linked_channel_ids),
                message_c.is_deleted.is_(False),
            )
            .order_by(message_c.channel_id, message_c.sent_at.desc())
        )

        # Filter out bot messages if requested
        if not include_bot_messages:
            message_stmt = message_stmt.where(message_c.is_bot_message.is_(False))

        # Filter by sender email or name (uses participant table already joined)
        if sender_email:
            message_stmt = message_stmt.where(
                message_c.author_email_normalized == sender_email.lower()
            )
        if sender_name:
            message_stmt = message_stmt.where(
                message_c.author_display_name.ilike(f"%{sender_name}%")
            )

        try:
            result = await session.execute(message_stmt)
            message_rows = result.fetchall()
        except Exception as e:
            logger.exception(f"list_deal_channel_messages: error fetching messages: {e}")
            return {"channels": [], "total_messages": 0, "total_channels": 0}

    # Distribute messages to channels with per-channel limit
    channel_msg_counts: dict[UUID, int] = dict.fromkeys(linked_channel_ids, 0)
    total_messages = 0

    for row in message_rows:
        message = row[0]
        author_display_name = row[1]
        author_email = row[2]

        channel_uuid = message.channel_id

        # Apply per-channel limit
        if channel_msg_counts[channel_uuid] >= messages_per_channel:
            continue

        sender_display = author_display_name or author_email or "Unknown"

        message_data: dict[str, Any] = {
            "message_id": str(message.message_id),
            "sender_name": sender_display,
            "sender_email": author_email,
            "content": message.content,
            "sent_at": message.sent_at.isoformat() if message.sent_at else None,
            "message_type": message.message_type.value
            if hasattr(message.message_type, "value")
            else str(message.message_type),
            "is_important": message.is_important,
            "has_attachments": bool(message.attachments),
            "is_bot_message": message.is_bot_message,
        }

        # Add thread info if available
        if message.thread_id:
            message_data["thread_id"] = message.thread_id

        # Add edited timestamp if message was edited
        if message.edited_at:
            message_data["edited_at"] = message.edited_at.isoformat()

        channels_info[channel_uuid]["messages"].append(message_data)
        channel_msg_counts[channel_uuid] += 1
        total_messages += 1

    # Build final output
    channels_list = list(channels_info.values())

    logger.info(
        f"list_deal_channel_messages: returning {total_messages} messages "
        f"across {len(channels_list)} channels for deal {actual_deal_id}"
    )

    return {
        "channels": channels_list,
        "total_messages": total_messages,
        "total_channels": len(channels_list),
    }


def create_list_deal_channel_messages(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create list_deal_channel_messages tool bound to a context."""

    async def list_deal_channel_messages_tool(
        deal_id: str | None = None,
        channel_id: str | None = None,
        messages_per_channel: int = 20,
        include_bot_messages: bool = False,
        sender_email: str | None = None,
        sender_name: str | None = None,
    ) -> dict[str, Any]:
        """List messages from messaging channels (Teams, Slack, etc.) linked to a deal.

        Returns messages organized by channel, making it clear which messages
        came from which channel. Each channel gets equal representation.
        Supports filtering by sender email or name.

        Args:
            deal_id: The deal UUID. If not provided, uses the current deal context.
            channel_id: Optional - filter to a specific channel UUID.
            messages_per_channel: Max messages per channel (default: 20).
                                  Ensures balanced representation across channels.
            include_bot_messages: Include messages sent by bots (default: False)
            sender_email: Filter messages sent by this email address
                (exact match, case-insensitive).
            sender_name: Filter messages where the sender's display name contains this string
                (case-insensitive partial match).

        Returns:
            Structured response with:
            - channels: List of channel objects, each containing:
                - channel_id: UUID of the channel
                - channel_name: Human-readable name
                - channel_type: public, private, direct_message, group_message
                - provider: teams, slack, google_chat
                - total_message_count: Total messages in channel (not just returned)
                - last_activity_at: When last message was sent
                - messages: List of messages, each with:
                    - sender_name: Who sent it
                    - content: Message text
                    - sent_at: Timestamp
            - total_messages: Count of messages returned
            - total_channels: Number of channels

        Example response:
            {
                "channels": [
                    {
                        "channel_id": "abc-123",
                        "channel_name": "Acme Deal Discussion",
                        "channel_type": "private",
                        "provider": "teams",
                        "messages": [
                            {"sender_name": "John", "content": "...", "sent_at": "..."},
                            ...
                        ]
                    },
                    {
                        "channel_id": "def-456",
                        "channel_name": "Acme Support",
                        ...
                    }
                ],
                "total_messages": 40,
                "total_channels": 2
            }
        """
        logger.info(
            f"[TOOL CALLED] list_deal_channel_messages - deal_id='{deal_id}', "
            f"channel_id='{channel_id}', messages_per_channel={messages_per_channel}"
        )

        if not context.organisation_id:
            logger.warning("list_deal_channel_messages unavailable: missing organisation_id")
            return {"channels": [], "total_messages": 0, "total_channels": 0}

        async with db_session_lock():
            result = await execute_with_capabilities(
                capabilities_getter(),
                tool_name="ListDealChannelMessages",
                tool_func=list_deal_channel_messages,
                parameters={
                    "context": context,
                    "deal_id": deal_id,
                    "channel_id": channel_id,
                    "messages_per_channel": messages_per_channel,
                    "include_bot_messages": include_bot_messages,
                    "sender_email": sender_email,
                    "sender_name": sender_name,
                },
                description="Reading channel messages",
                fallback_value={"channels": [], "total_messages": 0, "total_channels": 0},
            )

        total = result.get("total_messages", 0) if isinstance(result, dict) else 0
        channels = result.get("total_channels", 0) if isinstance(result, dict) else 0
        logger.info(
            f"[TOOL RESULT] list_deal_channel_messages returned "
            f"{total} messages across {channels} channels"
        )
        return dict(result) if isinstance(result, dict) else result

    return list_deal_channel_messages_tool
