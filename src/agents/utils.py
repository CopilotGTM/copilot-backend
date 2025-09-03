from __future__ import annotations
from typing import List
from agno.agent.agent import Message
from src.domain.models.organisation_tables import ChatMessage

def prepare_messages(message_list: List[ChatMessage]) -> List[Message]:
    """Prepare messages for the agent by extracting relevant content."""
    processed_messages = []
    for message in message_list:
        content = message.content
        if isinstance(content, dict):
            content = content.get("text", "")
        message_dict = Message(role=message.role, content=content)
        processed_messages.append(message_dict)
    return processed_messages