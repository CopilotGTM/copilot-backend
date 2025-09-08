from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, List

from src.api.schemas.chat_stream_models import Frame
from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from src.agents.models import ChatAgentOutput


class Agent(ABC):
    async def run_structured(self, message_list: List[ChatMessage]) -> ChatAgentOutput:
        """Run the agent once and return a structured output model."""
        raise NotImplementedError

    @abstractmethod
    async def run_stream(self, message_list: List[ChatMessage]) -> AsyncIterator[Frame]:
        """Run the agent and stream back frames."""
        ...
