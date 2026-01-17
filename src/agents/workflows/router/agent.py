from __future__ import annotations

from collections.abc import AsyncIterator

from src.agents.base import Agent
from src.agents.context import AgentContext
from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from src.infra.messaging.chat_stream_models import Frame


class RouterAgent(Agent):
    """Agent that routes to the correct sub-agent."""

    def __init__(self, *, context: AgentContext):
        super().__init__(context=context)

    async def run_stream(self, message_list: list[ChatMessage]) -> AsyncIterator[Frame]:
        """Run the agent and stream back frames."""
        # TODO: Add logic to determine which agent to run
        from src.agents.registry import resolve_agent

        agent_cls = resolve_agent("general_chat")
        agent = agent_cls(context=self.context)
        async for frame in agent.run_stream(message_list):
            yield frame
