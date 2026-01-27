from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agno.models.message import Message

from src.domain.models.organisation_tables import ChatMessage


def prepare_messages(message_list: Sequence[ChatMessage | dict[str, Any]]) -> list[Message]:
    """Prepare messages for the agent by extracting relevant content.

    Handles both ChatMessage objects and dict representations.
    """
    processed_messages = []
    for message in message_list:
        # Handle both dict and ChatMessage object formats
        if isinstance(message, dict):
            content = message.get("content", "")
            role = message.get("role", "user")
        else:
            content = message.content
            role = message.role

        # Handle nested content structure
        if isinstance(content, dict):
            content = content.get("text", "")

        message_dict = Message(role=role, content=content)
        processed_messages.append(message_dict)
    return processed_messages
