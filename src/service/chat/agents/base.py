from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from .models import ChatAgentOutput


class Agent(ABC):
    @abstractmethod
    async def run_structured(self, message_list: List[ChatMessage]) -> ChatAgentOutput:
        """Run the agent once and return a structured output model."""
        ...
