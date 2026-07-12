from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from src.agents.context import AgentContext
from src.agents.context_resolution import resolve_context_references
from src.agents.utils import latest_user_text
from src.agents.workflows.general.agent import GeneralAgent
from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from src.domain.models.organisation_tables.chat_session_models import ChatType
from src.infra.messaging.chat_stream_models import Frame
from src.infra.utils.agent_datetime import extract_temporal_hints


async def run_agent_for_chat(
    *,
    chat_type: ChatType | str,
    messages: Sequence[ChatMessage | dict[str, Any]],
    context: AgentContext,
    images: Sequence[Any] | None = None,
    attachment_text: str | None = None,
) -> AsyncIterator[Frame]:
    """Run the agent for the given chat type and stream back the results.

    SECURITY: The context contains immutable organisation_id and deal_id that
    cannot be modified by agents. This prevents cross-org/cross-deal data access.
    Sessions are acquired on-demand via context.acquire_session() to avoid holding
    database connections during long-running LLM calls.

    Args:
        chat_type: Type of chat/agent to run. Kept for API stability even though
            all current interactive chat flows use GeneralAgent.
        messages: Conversation history (can be ChatMessage objects or dicts)
        context: AgentContext object with immutable security boundaries
        images: Optional Agno Image objects for the current user message
        attachment_text: Optional extracted text from attachments (PDF, DOCX, audio transcript)

    Yields:
        Frame objects representing agent output
    """
    _ = chat_type
    user_text = latest_user_text(messages)
    temporal_hints = extract_temporal_hints(user_text)
    if user_text or temporal_hints:
        metadata: dict[str, Any] = {"latest_user_text": user_text}
        if temporal_hints.get("direction"):
            metadata["temporal_direction"] = temporal_hints["direction"]
        if temporal_hints.get("limit"):
            metadata["temporal_limit"] = temporal_hints["limit"]
        context = context.with_metadata(**metadata)

    resolution = await resolve_context_references(context=context, latest_user_text=user_text)
    if resolution:
        context = context.with_metadata(context_resolution=resolution)

    agent = GeneralAgent(context=context)
    async for frame in agent.run_stream(messages, images=images, attachment_text=attachment_text):
        yield frame
