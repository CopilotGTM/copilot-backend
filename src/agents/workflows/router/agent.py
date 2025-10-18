from __future__ import annotations

from collections.abc import AsyncIterator

from src.agents.base import Agent
from src.api.schemas.chat_stream_models import Frame
from src.domain.models.organisation_tables.chat_message_models import ChatMessage


class RouterAgent(Agent):
    """Agent that routes to the correct sub-agent."""

    async def run_stream(self, message_list: list[ChatMessage]) -> AsyncIterator[Frame]:
        """Run the agent and stream back frames."""
        # TODO: Add logic to determine which agent to run
        from src.agents.registry import resolve_agent

        agent_cls = resolve_agent("general_chat")
        agent = agent_cls()
        async for frame in agent.run_stream(message_list):
            yield frame
