from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.context import AgentContext
from src.agents.registry import resolve_agent
from src.api.schemas.chat_stream_models import Frame
from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from src.domain.models.organisation_tables.chat_session_models import ChatType


async def run_agent_for_chat(
    *,
    chat_type: ChatType | str,
    messages: list[ChatMessage],
    session: AsyncSession | None = None,
    organisation_id: UUID | None = None,
    user_id: UUID | None = None,
    context: AgentContext | None = None,
) -> AsyncIterator[Frame]:
    """Run the agent for the given chat type and stream back the results.

    Args:
        chat_type: Type of chat/agent to run
        messages: Conversation history
        session: Database session (legacy parameter, ignored if context provided)
        organisation_id: Organisation UUID (legacy parameter, ignored if context provided)
        user_id: User UUID (legacy parameter, ignored if context provided)
        context: AgentContext object (preferred method)

    Yields:
        Frame objects representing agent output
    """
    # Build context if not provided (backward compatibility)
    if not context:
        if not session or not organisation_id:
            raise ValueError("Either context or (session + organisation_id) required")
        context = AgentContext(
            session=session,
            organisation_id=organisation_id,
            user_id=user_id,
        )

    agent_cls = resolve_agent(chat_type)
    agent = agent_cls(context=context)
    async for frame in agent.run_stream(messages):
        yield frame
