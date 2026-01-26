from __future__ import annotations

from collections.abc import AsyncIterator

from src.agents.context import AgentContext
from src.agents.registry import resolve_agent
from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from src.domain.models.organisation_tables.chat_session_models import ChatType
from src.infra.messaging.chat_stream_models import Frame


async def run_agent_for_chat(
    *,
    chat_type: ChatType | str,
    messages: list[ChatMessage],
    context: AgentContext,
) -> AsyncIterator[Frame]:
    """Run the agent for the given chat type and stream back the results.

    SECURITY: The context contains immutable organisation_id and deal_id that
    cannot be modified by agents. This prevents cross-org/cross-deal data access.
    Sessions are acquired on-demand via context.acquire_session() to avoid holding
    database connections during long-running LLM calls.

    Args:
        chat_type: Type of chat/agent to run
        messages: Conversation history
        context: AgentContext object with immutable security boundaries

    Yields:
        Frame objects representing agent output
    """
    agent_cls = resolve_agent(chat_type)
    agent = agent_cls(context=context)
    async for frame in agent.run_stream(messages):
        yield frame
