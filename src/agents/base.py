from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.context import AgentContext

from src.agents.models import ChatAgentOutput
from src.api.schemas.chat_stream_models import Frame
from src.domain.models.organisation_tables.chat_message_models import ChatMessage


class Agent(ABC):
    def __init__(self, *, context: AgentContext):
        self.context = context

    async def run_structured(self, message_list: list[ChatMessage]) -> ChatAgentOutput:
        """Run the agent once and return a structured output model."""
        raise NotImplementedError

    @abstractmethod
    def run_stream(self, message_list: list[ChatMessage]) -> AsyncIterator[Frame]:
        """Run the agent and stream back frames."""
        ...
