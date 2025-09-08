from __future__ import annotations

from typing import AsyncIterator, List

from src.api.schemas.chat_stream_models import Frame
from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from src.domain.models.organisation_tables.chat_session_models import ChatType

from src.agents.registry import resolve_agent


async def run_agent_for_chat(
    *, chat_type: ChatType | str, messages: List[ChatMessage]
) -> AsyncIterator[Frame]:
    """Run the agent for the given chat type and stream back the results."""
    agent_cls = resolve_agent(chat_type)
    agent = agent_cls()
    async for frame in agent.run_stream(messages):
        yield frame
