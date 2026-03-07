"""Bot message processing service for chat worker.

Processes messages from bot platforms (Teams, Slack) through the GeneralAgent
and sends the accumulated response back to the originating platform.

Personal chat messages are persisted in the database (chat_sessions/chat_messages
with chat_type=TEAMS_CHAT) and history is loaded from DB for context.
Group chat messages are NOT persisted; the agent calls fetch_teams_chat_messages.

Follows the fresh session pattern: no DB sessions are held during LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.base.worker_utils import get_organisation_name

if TYPE_CHECKING:
    from src.adapters import ChannelPayload
    from src.infra.messaging.chat_stream_models import Frame

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

    For personal chats, messages are persisted in chat_sessions/chat_messages
    (chat_type=TEAMS_CHAT) and history is loaded from DB.
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
        user_email: str | None = None,
    ) -> bool:
        """Process a bot message and send reply.

        Args:
            user_id: The linked CopilotGTM user ID
            message_text: The user's message text
            provider: Bot platform identifier (e.g. "teams")
            reply_context: Platform-specific context for sending the reply
            chat_history: Optional previous conversation messages
            bot_context: Optional context for bot-specific features (e.g. group chat metadata)
            user_email: The email of the user who sent the message

        Returns:
            True if processing and reply succeeded, False otherwise
        """
        try:
            from src.adapters import AgentResponse, get_adapter

            # For personal chat, persist user message and load history from DB
            session_id: UUID | None = None
            user_msg_id: UUID | None = None
            conversation_type = (bot_context or {}).get("conversation_type", "personal")

            if conversation_type == "personal":
                session_id = self._get_or_create_bot_session(user_id, provider)
                user_msg_id = self._persist_user_message(session_id, message_text)
                chat_history = self._load_history(session_id)

            result = run_async(
                self._run_agent_and_accumulate(
                    user_id,
                    message_text,
                    chat_history,
                    bot_context=bot_context,
                    user_email=user_email,
                    reply_context=reply_context,
                )
            )

            adapter = get_adapter(provider)
            if result.error_message:
                payload = adapter.render_error(result.error_message)
            else:
                response = AgentResponse.from_frames(result.frames)
                payload = adapter.render(response)

                # Persist assistant response for personal chat
                if session_id and user_msg_id and not result.error_message:
                    assistant_text = response.text
                    if assistant_text:
                        self._persist_assistant_message(session_id, assistant_text, user_msg_id)

            success = run_async(self._send_payload(provider, reply_context, payload))

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
                run_async(self._send_payload(provider, reply_context, payload))
            except Exception:
                logger.warning("Failed to send error reply to bot platform")

            return False

    # ------------------------------------------------------------------
    # Personal chat DB persistence
    # ------------------------------------------------------------------

    def _get_or_create_bot_session(self, user_id: UUID, provider: str = "teams") -> UUID:
        """Get or create a bot chat session for this user+org.

        Each user gets one session per provider per org, reused across all
        personal messages.  Uses try/except on IntegrityError for the race
        condition where two concurrent messages try to create the session.
        """
        from sqlalchemy.exc import IntegrityError
        from sqlmodel import col, select

        from src.domain.models.organisation_tables.chat_session_models import (
            ChatSession,
            ChatType,
        )
        from src.infra.database.sessions import org_session_scope

        chat_type = ChatType.SLACK_CHAT if provider == "slack" else ChatType.TEAMS_CHAT
        session_title = "Slack Chat" if provider == "slack" else "Teams Chat"

        with org_session_scope(self.organisation_id) as session:
            stmt = select(ChatSession).where(
                col(ChatSession.user_id) == user_id,
                col(ChatSession.organisation_id) == self.organisation_id,
                col(ChatSession.chat_type) == chat_type,
                col(ChatSession.is_deleted).is_(False),
            )
            existing = session.execute(stmt).scalars().first()
            if existing:
                return existing.session_id

        # Create outside the read session to avoid holding it
        with org_session_scope(self.organisation_id) as session:
            new_session = ChatSession(
                user_id=user_id,
                organisation_id=self.organisation_id,
                chat_type=chat_type,
                title=session_title,
            )
            session.add(new_session)
            try:
                session.commit()
                logger.info(
                    "Created %s session=%s for user=%s org=%s",
                    chat_type.value,
                    new_session.session_id,
                    user_id,
                    self.organisation_id,
                )
                return new_session.session_id
            except IntegrityError:
                session.rollback()
                # Race condition: another request created it first, re-query
                stmt = select(ChatSession).where(
                    col(ChatSession.user_id) == user_id,
                    col(ChatSession.organisation_id) == self.organisation_id,
                    col(ChatSession.chat_type) == chat_type,
                    col(ChatSession.is_deleted).is_(False),
                )
                existing = session.execute(stmt).scalars().first()
                if existing:
                    return existing.session_id
                raise  # Unexpected error, re-raise

    def _persist_user_message(self, session_id: UUID, text: str) -> UUID:
        """Persist a user message to the DB and return its message_id."""
        from src.domain.models.organisation_tables.chat_message_models import (
            ChatMessage,
            ChatMessageRole,
            ChatMessageType,
        )
        from src.infra.database.sessions import org_session_scope

        with org_session_scope(self.organisation_id) as session:
            msg = ChatMessage(
                session_id=session_id,
                role=ChatMessageRole.USER,
                message_type=ChatMessageType.GENERAL_MESSAGE,
                content={"text": text},
            )
            session.add(msg)
            session.commit()
            return msg.message_id

    def _persist_assistant_message(
        self,
        session_id: UUID,
        text: str,
        parent_message_id: UUID,
    ) -> None:
        """Persist the assistant response to the DB."""
        from src.domain.models.organisation_tables.chat_message_models import (
            ChatMessage,
            ChatMessageRole,
            ChatMessageType,
        )
        from src.infra.database.sessions import org_session_scope

        with org_session_scope(self.organisation_id) as session:
            msg = ChatMessage(
                session_id=session_id,
                role=ChatMessageRole.ASSISTANT,
                message_type=ChatMessageType.GENERAL_MESSAGE,
                content={"text": text},
                parent_message_id=parent_message_id,
            )
            session.add(msg)
            session.commit()

    def _load_history(self, session_id: UUID, limit: int = 20) -> list[dict] | None:
        """Load recent chat history from the DB for this session.

        Returns messages in chronological order (oldest first).
        Strips the last user message (the current one just persisted)
        since _build_messages adds it separately.
        """
        from sqlmodel import col, select

        from src.domain.models.organisation_tables.chat_message_models import (
            ChatMessage,
        )
        from src.infra.database.sessions import org_session_scope

        with org_session_scope(self.organisation_id) as session:
            stmt = (
                select(ChatMessage)
                .where(col(ChatMessage.session_id) == session_id)
                .order_by(col(ChatMessage.created_at).desc())
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()

        if not rows:
            return None

        # Reverse to chronological order (oldest first)
        messages = list(reversed(rows))

        # Strip the last user message (the one we just persisted)
        if messages and messages[-1].role == "user":
            messages = messages[:-1]

        if not messages:
            return None

        return [
            {"role": msg.role, "content": (msg.content or {}).get("text", "")} for msg in messages
        ]

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent_and_accumulate(
        self,
        user_id: UUID,
        message_text: str,
        chat_history: list[dict] | None = None,
        bot_context: dict | None = None,
        user_email: str | None = None,
        reply_context: dict | None = None,
    ) -> BotAgentResult:
        """Run agent and accumulate frames.

        Creates an AgentContext with org_id + user_id (fresh session pattern),
        iterates over streamed frames, and accumulates them.

        For personal chats, history is loaded from the DB before this method is called.
        For group chats, the agent calls the fetch_teams_chat_messages tool itself.

        Args:
            reply_context: Platform reply context (service_url, conversation_id) -
                passed into metadata so tools can send proactive cards.
        """
        from src.agents.context import AgentContext
        from src.agents.orchestrator import run_agent_for_chat

        result = BotAgentResult()

        conversation_type = (bot_context or {}).get("conversation_type", "personal")

        messages = self._build_messages(message_text, chat_history, bot_context=bot_context)

        # Determine channel from reply_context or bot_context
        channel = "teams"
        if reply_context and "channel_id" in reply_context and "service_url" not in reply_context:
            channel = "slack"

        metadata: dict = {}
        if bot_context and conversation_type == "groupChat":
            metadata["conversation_type"] = "groupChat"
            metadata["teams_conversation_id"] = bot_context.get("teams_conversation_id", "")
        elif bot_context and conversation_type == "channel":
            metadata["conversation_type"] = "channel"
            metadata["slack_channel_id"] = bot_context.get("slack_channel_id", "")

        # Pass reply context so tools can send proactive cards (e.g. create_deal form)
        if reply_context:
            metadata["service_url"] = reply_context.get("service_url", "")
            metadata["conversation_id"] = reply_context.get("conversation_id", "")
            metadata["channel_id"] = reply_context.get("channel_id", "")
            metadata["team_id"] = reply_context.get("team_id", "")

        # Pass user timezone if available so tools don't need to ask
        user_tz = (bot_context or {}).get("user_timezone", "")
        if user_tz:
            metadata["user_timezone"] = user_tz

        # Look up real org name from master (public) schema
        org_name = "Our Organization"
        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id) or org_name

        context = AgentContext(
            organisation_id=self.organisation_id,
            org_name=org_name,
            user_id=user_id,
            user_email=user_email,
            channel=channel,
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
        elif bot_context and bot_context.get("conversation_type") == "channel":
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "You are responding to an @mention in a Slack channel. "
                        "You do NOT have the channel history in your conversation context. "
                        "Do not claim you have read prior channel messages. "
                        "If context is missing, ask a brief clarifying question before answering. "
                        "For CRM write requests (for example, creating a deal), "
                        "call the write tool immediately once required fields are present. "
                        "Do not ask follow-up questions for optional fields before calling tools."
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
        elif provider == "slack":
            return await self._send_slack_payload(reply_context, payload)
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

    async def _send_slack_payload(self, reply_context: dict, payload: ChannelPayload) -> bool:
        """Send rendered payload to Slack via Web API.

        If reply_context contains a thinking_message_ts, the thinking placeholder
        is updated in-place via chat.update instead of posting a new message.
        """
        from src.service.bots.slack_bot_service import SlackBotService

        channel_id = reply_context.get("channel_id", "")
        team_id = reply_context.get("team_id", "")
        thinking_ts = reply_context.get("thinking_message_ts", "")

        if not channel_id or not team_id:
            logger.error(
                "Missing Slack reply context: channel_id=%s team_id=%s",
                bool(channel_id),
                bool(team_id),
            )
            return False

        bot_service = SlackBotService()
        blocks = payload.content.get("blocks")
        text = payload.content.get("text") or payload.text_fallback or ""

        # Update thinking placeholder in-place if we have a ts
        if thinking_ts:
            if blocks:
                updated = await bot_service._update_message_blocks(
                    team_id=team_id,
                    channel_id=channel_id,
                    message_ts=thinking_ts,
                    blocks=blocks,
                    text_fallback=text,
                )
            else:
                updated = await bot_service._update_message(
                    team_id=team_id,
                    channel_id=channel_id,
                    message_ts=thinking_ts,
                    text=text,
                )
            if updated:
                return True
            # Fallback: update failed, post as a new message instead
            logger.warning("chat.update failed, falling back to new message")

        # Post a new message (or fallback from failed update)
        if blocks:
            return await bot_service.send_block_kit_message(
                team_id=team_id,
                channel_id=channel_id,
                blocks=blocks,
                text_fallback=text,
            )
        else:
            return await bot_service._send_message(team_id, channel_id, text)
