from __future__ import annotations

from typing import List

from src.domain.models.organisation_tables.chat_session_models import ChatType
from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from .agents.registry import resolve_agent
from .agents.models import ChatAgentOutput


async def run_agent_for_chat_structured(
    *,
    chat_type: ChatType | str,
    messages: List[ChatMessage],
) -> ChatAgentOutput:
    AgentCls = resolve_agent(chat_type)
    agent = AgentCls()
    return await agent.run_structured(messages)
