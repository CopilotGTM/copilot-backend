"""Bot message processing service for chat worker.

Processes messages from bot platforms (Teams, Slack) through the GeneralAgent
and sends the accumulated response back to the originating platform.

Unlike MessageProcessingService, this service:
- Does NOT persist chat messages to the database
- Does NOT publish frames to Redis Streams (no SSE)
- Sends the final reply directly to the bot platform via proactive messaging

Follows the fresh session pattern: no DB sessions are held during LLM calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from src.infra.utils.logger import get_logger

logger = get_logger("bot_message_processing_service")

# Frame type constants (subset relevant to bot reply accumulation)
FRAME_TYPE_ANSWER_DELTA = "answer.delta"
FRAME_TYPE_ANSWER_REPLACE = "answer.replace"
FRAME_TYPE_SOURCES_REPLACE = "sources.replace"
FRAME_TYPE_ERROR = "error"
FRAME_TYPE_FINALIZE = "finalize"


@dataclass
class BotAgentResult:
    """Accumulated result from agent execution for bot reply."""

    answer_parts: list[str] = field(default_factory=list)
    final_answer: str | None = None
    sources: list[dict] | None = None
    error_message: str | None = None


class BotMessageProcessingService:
    """Service for processing bot messages through the GeneralAgent.

    Flow:
    1. Build message history from task args
    2. Run GeneralAgent via run_agent_for_chat() (no DB session held)
    3. Accumulate streaming frames into a single response
    4. Format and send reply via the bot platform's proactive messaging API
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id

    def process(
        self,
        user_id: UUID,
        message_text: str,
        provider: str,
        reply_context: dict,
        chat_history: list[dict] | None = None,
        bot_context: dict | None = None,
    ) -> bool:
        """Process a bot message and send reply.

        Args:
            user_id: The linked CopilotGTM user ID
            message_text: The user's message text
            provider: Bot platform identifier (e.g. "teams")
            reply_context: Platform-specific context for sending the reply
            chat_history: Optional previous conversation messages
            bot_context: Optional context for bot-specific features (e.g. group chat metadata)

        Returns:
            True if processing and reply succeeded, False otherwise
        """
        try:
            # Run agent and accumulate response
            result = asyncio.run(
                self._run_agent_and_accumulate(
                    user_id, message_text, chat_history, bot_context=bot_context
                )
            )

            # Format the reply
            reply_text = self._format_bot_reply(result)

            # Send reply to bot platform
            success = asyncio.run(self._send_reply(provider, reply_context, reply_text))

            if success:
                logger.info(
                    "Bot message processed successfully org=%s user=%s provider=%s",
                    self.organisation_id,
                    user_id,
                    provider,
                )
            else:
                logger.error(
                    "Failed to send bot reply org=%s user=%s provider=%s",
                    self.organisation_id,
                    user_id,
                    provider,
                )

            return success

        except Exception as e:
            logger.exception(
                "Bot message processing failed org=%s user=%s error=%s",
                self.organisation_id,
                user_id,
                e,
            )
            # Attempt to send error message back to user
            try:
                error_reply = (
                    "I'm sorry, something went wrong while processing your request. "
                    "Please try again or use the CopilotGTM web app."
                )
                asyncio.run(self._send_reply(provider, reply_context, error_reply))
            except Exception:
                logger.warning("Failed to send error reply to bot platform")

            return False

    async def _run_agent_and_accumulate(
        self,
        user_id: UUID,
        message_text: str,
        chat_history: list[dict] | None = None,
        bot_context: dict | None = None,
    ) -> BotAgentResult:
        """Run agent and accumulate frames into a single response.

        Creates an AgentContext with org_id + user_id (fresh session pattern),
        iterates over streamed frames, and accumulates them into BotAgentResult.
        """
        from src.agents.context import AgentContext
        from src.agents.orchestrator import run_agent_for_chat

        result = BotAgentResult()

        # Build message list for the agent
        messages = self._build_messages(message_text, chat_history, bot_context=bot_context)

        # Build metadata from bot_context (e.g. group chat conversation ID)
        metadata: dict = {}
        if bot_context and bot_context.get("conversation_type") == "groupChat":
            metadata["conversation_type"] = "groupChat"
            metadata["teams_conversation_id"] = bot_context.get("teams_conversation_id", "")

        # Create agent context (no DB session held)
        context = AgentContext(
            organisation_id=self.organisation_id,
            org_name="CopilotGTM",
            user_id=user_id,
            metadata=metadata,
        )

        try:
            async for frame in run_agent_for_chat(
                chat_type="GENERAL_CHAT",
                messages=messages,
                context=context,
            ):
                try:
                    frame_data = frame.model_dump()
                except Exception:
                    frame_data = getattr(frame, "dict", lambda f=frame: dict(f))()

                self._accumulate_frame(frame_data, result)

        except Exception as e:
            logger.exception("Agent execution failed: %s", e)
            result.error_message = str(e)

        return result

    def _build_messages(
        self,
        message_text: str,
        chat_history: list[dict] | None = None,
        bot_context: dict | None = None,
    ) -> list[dict]:
        """Build message list for the agent.

        Constructs a list of message dicts compatible with run_agent_for_chat.
        Includes optional chat history followed by the current user message.

        For group chat contexts, prepends a system message instructing the agent
        to use the fetch_teams_chat_messages tool to read conversation history.
        """
        messages: list[dict] = []

        # For group chat, prepend a system hint so the agent knows to fetch chat history
        if bot_context and bot_context.get("conversation_type") == "groupChat":
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "You are responding to an @mention in a Microsoft Teams group chat. "
                        "You do NOT have the chat history in your conversation context. "
                        "To understand what the group has been discussing, you MUST call the "
                        "fetch_teams_chat_messages tool FIRST before answering. "
                        "This tool fetches recent messages from the group chat so you can "
                        "provide a relevant and informed response."
                    ),
                }
            )

        if chat_history:
            messages.extend(chat_history)

        # Add the current user message
        messages.append(
            {
                "role": "user",
                "content": message_text,
            }
        )

        return messages

    def _accumulate_frame(self, frame_data: dict, result: BotAgentResult) -> None:
        """Accumulate a single frame into the result."""
        ftype = frame_data.get("type")

        if ftype == FRAME_TYPE_ANSWER_DELTA:
            result.answer_parts.append(frame_data.get("content") or "")
        elif ftype == FRAME_TYPE_ANSWER_REPLACE:
            result.final_answer = frame_data.get("content") or ""
        elif ftype == FRAME_TYPE_SOURCES_REPLACE:
            result.sources = frame_data.get("sources")
        elif ftype == FRAME_TYPE_ERROR:
            result.error_message = frame_data.get("message", "Unknown error")
        # All other frame types (tool.usage, thinking.update, progress.update,
        # visualization, structured_data, task_list, suggestions.replace) are
        # skipped - not relevant for a single-message bot reply.

    def _format_bot_reply(self, result: BotAgentResult) -> str:
        """Format accumulated agent result into bot-friendly markdown."""
        if result.error_message and not result.answer_parts and not result.final_answer:
            return (
                "I'm sorry, I encountered an issue while processing your request. "
                "Please try again or use the CopilotGTM web app."
            )

        # Use final_answer (from answer.replace) if available, otherwise join deltas
        answer = result.final_answer or "".join(result.answer_parts)

        if not answer.strip():
            return "I wasn't able to generate a response. Please try rephrasing your question."

        # Append sources as numbered references if available
        if result.sources:
            source_lines = []
            for i, source in enumerate(result.sources, 1):
                label = source.get("label") or source.get("title") or f"Source {i}"
                url = source.get("url")
                if url:
                    source_lines.append(f"{i}. [{label}]({url})")
                else:
                    source_lines.append(f"{i}. {label}")

            if source_lines:
                answer += "\n\n---\n**Sources:**\n" + "\n".join(source_lines)

        return answer

    async def _send_reply(self, provider: str, reply_context: dict, message: str) -> bool:
        """Dispatch reply to the correct bot platform service."""
        if provider == "teams":
            return await self._send_teams_reply(reply_context, message)
        else:
            logger.error("Unsupported bot provider: %s", provider)
            return False

    async def _send_teams_reply(self, reply_context: dict, message: str) -> bool:
        """Send reply to Teams via proactive messaging."""
        from src.service.bots.teams_bot_service import TeamsBotService

        service_url = reply_context.get("service_url", "")
        conversation_id = reply_context.get("conversation_id", "")

        if not service_url or not conversation_id:
            logger.error(
                "Missing Teams reply context: service_url=%s conversation_id=%s",
                bool(service_url),
                bool(conversation_id),
            )
            return False

        bot_service = TeamsBotService()
        return await bot_service.send_proactive_message(
            service_url=service_url,
            conversation_id=conversation_id,
            message=message,
        )
