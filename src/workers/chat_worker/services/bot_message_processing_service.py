"""Bot message processing service for chat worker.

Processes messages from bot platforms (Teams, Slack) through the GeneralAgent
and sends the accumulated response back to the originating platform.

Unlike MessageProcessingService, this service:
- Does NOT persist chat messages to the database
- Does NOT publish frames to Redis Streams (no SSE)
- Sends the final reply directly to the bot platform via proactive messaging
- Uses channel adapters for channel-specific rendering (Adaptive Cards, Block Kit, etc.)

Follows the fresh session pattern: no DB sessions are held during LLM calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from src.adapters import AgentResponse, ChannelPayload, get_adapter
from src.infra.messaging.chat_stream_models import Frame
from src.infra.utils.logger import get_logger

logger = get_logger("bot_message_processing_service")

FRAME_TYPE_FINALIZE = "finalize"


@dataclass
class BotAgentResult:
    """Accumulated frames from agent execution for bot reply."""

    frames: list[Frame] = field(default_factory=list)
    error_message: str | None = None


class BotMessageProcessingService:
    """Service for processing bot messages through the GeneralAgent.

    Flow:
    1. Build message history from task args
    2. Run GeneralAgent via run_agent_for_chat() (no DB session held)
    3. Accumulate streaming frames
    4. Use channel adapter to render frames into channel-specific payload
    5. Send reply via the bot platform's proactive messaging API
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
            result = asyncio.run(
                self._run_agent_and_accumulate(
                    user_id, message_text, chat_history, bot_context=bot_context
                )
            )

            adapter = get_adapter(provider)
            if result.error_message:
                payload = adapter.render_error(result.error_message)
            else:
                response = AgentResponse.from_frames(result.frames)
                payload = adapter.render(response)

            success = asyncio.run(self._send_payload(provider, reply_context, payload))

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
            try:
                adapter = get_adapter(provider)
                payload = adapter.render_error(
                    "I'm sorry, something went wrong while processing your request. "
                    "Please try again or use the CopilotGTM web app."
                )
                asyncio.run(self._send_payload(provider, reply_context, payload))
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
        """Run agent and accumulate frames.

        Creates an AgentContext with org_id + user_id (fresh session pattern),
        iterates over streamed frames, and accumulates them.
        """
        from src.agents.context import AgentContext
        from src.agents.orchestrator import run_agent_for_chat

        result = BotAgentResult()

        messages = self._build_messages(message_text, chat_history, bot_context=bot_context)

        metadata: dict = {}
        if bot_context and bot_context.get("conversation_type") == "groupChat":
            metadata["conversation_type"] = "groupChat"
            metadata["teams_conversation_id"] = bot_context.get("teams_conversation_id", "")

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
                result.frames.append(frame)

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
        """Build message list for the agent."""
        messages: list[dict] = []

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

        messages.append(
            {
                "role": "user",
                "content": message_text,
            }
        )

        return messages

    async def _send_payload(
        self, provider: str, reply_context: dict, payload: ChannelPayload
    ) -> bool:
        """Dispatch rendered payload to the correct bot platform service."""
        if provider == "teams":
            return await self._send_teams_payload(reply_context, payload)
        else:
            logger.error("Unsupported bot provider: %s", provider)
            return False

    async def _send_teams_payload(self, reply_context: dict, payload: ChannelPayload) -> bool:
        """Send rendered payload to Teams via proactive messaging."""
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
        return await bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=payload.content,
            text_fallback=payload.text_fallback,
        )
