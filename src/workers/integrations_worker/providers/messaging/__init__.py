"""Messaging providers for Teams, Slack, etc."""

from .slack import (
    build_slack_channel_external_id,
    split_slack_channel_external_id,
)
from .slack import (
    fetch_channels as fetch_slack_channels,
)
from .slack import (
    fetch_chat_messages as fetch_slack_chat_messages,
)
from .slack import (
    fetch_chats as fetch_slack_chats,
)
from .slack import (
    fetch_messages as fetch_slack_messages,
)
from .teams import (
    FetchChatsResult,
    FetchMessagesResult,
    fetch_channels,
    fetch_chat_messages,
    fetch_chats,
    fetch_messages,
)

__all__ = [
    "fetch_channels",
    "fetch_chats",
    "fetch_messages",
    "fetch_chat_messages",
    "fetch_slack_channels",
    "fetch_slack_chats",
    "fetch_slack_messages",
    "fetch_slack_chat_messages",
    "build_slack_channel_external_id",
    "split_slack_channel_external_id",
    "FetchChatsResult",
    "FetchMessagesResult",
]
