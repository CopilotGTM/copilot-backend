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

# Minimum interval between thinking-indicator updates (seconds)
_STATUS_UPDATE_THROTTLE_SECONDS = 2.0

# Static error message shown when processing fails
_STATIC_ERROR_MESSAGE = (
    "I'm sorry, something went wrong while processing your request. "
    "Please try again or use the CopilotGTM web app."
)


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
        job_id: UUID | None = None,
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
            job_id: Pre-created job (PENDING) from bot service dispatch site

        Returns:
            True if processing and reply succeeded, False otherwise
        """
        # Track whether the thinking indicator was replaced with a response.
        # If not, the finally block sends a static error so it never lingers.
        thinking_replaced = False

        # Mark job as IN_PROGRESS
        if job_id:
            self._mark_job_in_progress(job_id)

        try:
            from src.adapters import AgentResponse, get_adapter

            conversation_type = (bot_context or {}).get("conversation_type", "personal")

            # For personal chats, session + user message were created at dispatch
            # time by the bot service. Load history from that session.
            session_id: UUID | None = None
            if job_id and conversation_type == "personal":
                session_id = self._get_session_from_job(job_id)
                if session_id:
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
                payload = adapter.render_error(_STATIC_ERROR_MESSAGE)
            else:
                response = AgentResponse.from_frames(result.frames)
                payload = adapter.render(response)

                # Persist assistant response for personal chat
                if session_id and job_id:
                    assistant_text = response.text
                    if assistant_text:
                        user_msg_id = self._get_user_message_from_job(job_id)
                        self._persist_assistant_message(
                            session_id, assistant_text, user_msg_id or UUID(int=0)
                        )

            success = run_async(self._send_payload(provider, reply_context, payload))

            # Finalize job with token usage data
            if job_id:
                self._finalize_job(job_id, result.frames, success=not result.error_message)

            if success:
                thinking_replaced = True
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
                payload = adapter.render_error(_STATIC_ERROR_MESSAGE)
                if run_async(self._send_payload(provider, reply_context, payload)):
                    thinking_replaced = True
            except Exception:
                logger.warning("Failed to send error reply to bot platform")

            return False

        finally:
            # Last resort: ensure thinking indicator never lingers
            if not thinking_replaced:
                try:
                    run_async(self._cleanup_thinking_indicator(provider, reply_context))
                except Exception:
                    logger.warning("Failed to clean up thinking indicator")

    # ------------------------------------------------------------------
    # Personal chat DB persistence
    # ------------------------------------------------------------------

    def _persist_assistant_message(
        self,
        session_id: UUID,
        text: str,
        parent_message_id: UUID,
    ) -> UUID:
        """Persist the assistant response to the DB and return its message_id."""
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
            return msg.message_id

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
    # Job tracking (token usage / cost)
    # ------------------------------------------------------------------

    def _mark_job_in_progress(self, job_id: UUID) -> None:
        """Mark a pre-created PENDING job as IN_PROGRESS."""
        from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
        from src.infra.database.sessions import org_session_scope

        with org_session_scope(self.organisation_id) as session:
            job = session.get(ChatMessageJob, job_id)
            if job:
                job.status = JobStatus.IN_PROGRESS
                session.add(job)
                session.commit()

    def _get_session_from_job(self, job_id: UUID) -> UUID | None:
        """Extract session_id from job metadata."""
        from src.domain.models.organisation_tables.job_models import ChatMessageJob
        from src.infra.database.sessions import org_session_scope

        with org_session_scope(self.organisation_id) as session:
            job = session.get(ChatMessageJob, job_id)
            if job and job.job_metadata:
                sid = job.job_metadata.get("session_id")
                return UUID(sid) if sid else None
        return None

    def _get_user_message_from_job(self, job_id: UUID) -> UUID | None:
        """Get the user message_id that the job is linked to."""
        from src.domain.models.organisation_tables.job_models import ChatMessageJob
        from src.infra.database.sessions import org_session_scope

        with org_session_scope(self.organisation_id) as session:
            job = session.get(ChatMessageJob, job_id)
            return job.message_id if job else None

    def _finalize_job(self, job_id: UUID, frames: list, success: bool) -> None:
        """Extract token usage from frames and persist to the job."""
        from decimal import Decimal

        from src.agents.common.token_tracking import extract_token_usage_from_frames
        from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
        from src.infra.database.sessions import org_session_scope

        token_usage, model_name, cost, duration = extract_token_usage_from_frames(frames)

        with org_session_scope(self.organisation_id) as session:
            job = session.get(ChatMessageJob, job_id)
            if not job:
                logger.warning("Bot job %s not found for finalization", job_id)
                return

            job.status = JobStatus.COMPLETED if success else JobStatus.FAILED
            if token_usage:
                job.token_usage = token_usage
            if model_name:
                job.model_name = model_name
            if cost is not None:
                job.cost = Decimal(str(cost))
            if duration is not None:
                job.latency_ms = int(duration * 1000)

            session.add(job)
            session.commit()

            logger.info(
                "Finalized bot job %s: status=%s, model=%s, cost=%s, latency_ms=%s",
                job_id,
                job.status,
                job.model_name,
                job.cost,
                job.latency_ms,
            )

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

        # For live status updates on the thinking indicator
        last_status_time = 0.0
        status_ctx = self._build_status_context(channel, reply_context or {})

        try:
            async for frame in run_agent_for_chat(
                chat_type="GENERAL_CHAT",
                messages=messages,
                context=context,
            ):
                result.frames.append(frame)

                # Update thinking indicator with tool status
                if (
                    status_ctx
                    and getattr(frame, "type", "") == "tool.usage"
                    and getattr(frame, "status", "") == "starting"
                ):
                    import time

                    now = time.time()
                    if now - last_status_time >= _STATUS_UPDATE_THROTTLE_SECONDS:
                        description = getattr(frame, "tool_description", "") or ""
                        if description:
                            await self._update_thinking_status(status_ctx, description)
                            last_status_time = now

        except Exception as e:
            logger.exception("Agent execution failed: %s", e)
            result.error_message = str(e)

        return result

    def _build_status_context(self, channel: str, reply_context: dict) -> dict | None:
        """Build platform-specific context for thinking-status updates."""
        if channel == "slack":
            thinking_ts = reply_context.get("thinking_message_ts")
            team_id = reply_context.get("team_id")
            channel_id = reply_context.get("channel_id")
            if thinking_ts and team_id and channel_id:
                return {
                    "platform": "slack",
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "thinking_ts": thinking_ts,
                }
        elif channel == "teams":
            service_url = reply_context.get("service_url")
            conversation_id = reply_context.get("conversation_id")
            if service_url and conversation_id:
                # Look up the thinking activity ID from Redis
                try:
                    from src.infra.messaging.redis_client import get_redis_client

                    redis = get_redis_client()
                    # Find the thinking activity ID by scanning recent keys
                    # The task ID isn't available here, so we use conversation_id
                    thinking_key = f"teams_thinking_conv:{conversation_id}"
                    activity_id = redis.get(thinking_key)
                    if activity_id:
                        return {
                            "platform": "teams",
                            "service_url": service_url,
                            "conversation_id": conversation_id,
                            "activity_id": activity_id.decode()
                            if isinstance(activity_id, bytes)
                            else activity_id,
                        }
                except Exception:
                    logger.debug("Failed to get Teams thinking activity ID", exc_info=True)
        return None

    async def _update_thinking_status(
        self,
        status_ctx: dict,
        status_text: str,
    ) -> None:
        """Update the thinking indicator with current tool status. Non-blocking."""
        try:
            platform = status_ctx["platform"]

            if platform == "slack":
                from src.service.bots.slack_bot_service import SlackBotService

                slack_bot = SlackBotService()
                await slack_bot._update_message(
                    team_id=status_ctx["team_id"],
                    channel_id=status_ctx["channel_id"],
                    message_ts=status_ctx["thinking_ts"],
                    text=f":hourglass_flowing_sand: _{status_text}..._",
                )

            elif platform == "teams":
                from src.service.bots.teams_bot_service import TeamsBotService

                teams_bot = TeamsBotService()
                await teams_bot.update_activity(
                    service_url=status_ctx["service_url"],
                    conversation_id=status_ctx["conversation_id"],
                    activity_id=status_ctx["activity_id"],
                    text=f"⏳ _{status_text}..._",
                )
        except Exception:
            # Non-critical — don't break agent flow if status update fails
            logger.debug("Failed to update thinking status", exc_info=True)

    async def _cleanup_thinking_indicator(
        self,
        provider: str,
        reply_context: dict,
    ) -> None:
        """Replace the thinking indicator with a static error message.

        Called as a last resort when all other send attempts failed, so the
        user never sees "Thinking..." stuck forever.
        """
        try:
            if provider == "slack":
                thinking_ts = reply_context.get("thinking_message_ts")
                team_id = reply_context.get("team_id")
                channel_id = reply_context.get("channel_id")
                thread_ts = reply_context.get("thread_ts")
                if thinking_ts and team_id and channel_id:
                    from src.service.bots.slack_bot_service import SlackBotService

                    slack_bot = SlackBotService()
                    updated = await slack_bot._update_message(
                        team_id=team_id,
                        channel_id=channel_id,
                        message_ts=thinking_ts,
                        text=f":warning: {_STATIC_ERROR_MESSAGE}",
                    )
                    if updated:
                        return
                    # Update failed — post new message so user isn't left hanging
                    await slack_bot._send_message(
                        team_id,
                        channel_id,
                        f":warning: {_STATIC_ERROR_MESSAGE}",
                        thread_ts=thread_ts,
                    )

            elif provider == "teams":
                service_url = reply_context.get("service_url")
                conversation_id = reply_context.get("conversation_id")
                if service_url and conversation_id:
                    activity_id = self._get_teams_thinking_activity_id(conversation_id)
                    if activity_id:
                        from src.service.bots.teams_bot_service import TeamsBotService

                        teams_bot = TeamsBotService()
                        updated = await teams_bot.update_activity(
                            service_url=service_url,
                            conversation_id=conversation_id,
                            activity_id=activity_id,
                            text=_STATIC_ERROR_MESSAGE,
                        )
                        if updated:
                            return
                    # Fallback: send new message
                    from src.service.bots.teams_bot_service import TeamsBotService

                    teams_fallback = TeamsBotService()
                    await teams_fallback.send_proactive_message(
                        service_url=service_url,
                        conversation_id=conversation_id,
                        message=_STATIC_ERROR_MESSAGE,
                    )
        except Exception:
            logger.warning("Thinking indicator cleanup failed", exc_info=True)

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
        """Send rendered payload to Teams via proactive messaging.

        If a thinking message was sent earlier, updates it in-place with the
        final answer. Otherwise posts a new message.
        """
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

        # Try to update the thinking message in-place with the final answer
        thinking_activity_id = self._get_teams_thinking_activity_id(conversation_id)
        if thinking_activity_id:
            updated = await bot_service.update_activity(
                service_url=service_url,
                conversation_id=conversation_id,
                activity_id=thinking_activity_id,
                payload=payload.content,
                text=payload.text_fallback,
            )
            if updated:
                return True
            # Update failed — try to blank the stale thinking message
            logger.warning("Teams thinking message update failed, posting new message")
            await bot_service.update_activity(
                service_url=service_url,
                conversation_id=conversation_id,
                activity_id=thinking_activity_id,
                text=" ",
            )

        return await bot_service.send_proactive_payload(
            service_url=service_url,
            conversation_id=conversation_id,
            payload=payload.content,
            text_fallback=payload.text_fallback,
        )

    def _get_teams_thinking_activity_id(self, conversation_id: str) -> str | None:
        """Look up the thinking message activity ID from Redis.

        Caches the result so cleanup can reuse it after the Redis key is deleted.
        """
        # Return cached value if we already retrieved it
        cached: str | None = getattr(self, "_cached_teams_thinking_id", None)
        if cached:
            return cached

        try:
            from src.infra.messaging.redis_client import get_redis_client

            redis = get_redis_client()
            key = f"teams_thinking_conv:{conversation_id}"
            activity_id = redis.get(key)
            if activity_id:
                redis.delete(key)
                decoded = (
                    activity_id.decode() if isinstance(activity_id, bytes) else str(activity_id)
                )
                self._cached_teams_thinking_id = decoded
                return decoded
        except Exception:
            logger.debug("Failed to get Teams thinking activity ID", exc_info=True)
        return None

    async def _send_slack_payload(self, reply_context: dict, payload: ChannelPayload) -> bool:
        """Send rendered payload to Slack via Web API.

        If reply_context contains a thinking_message_ts, the thinking placeholder
        is updated in-place via chat.update instead of posting a new message.

        If the Slack workspace rejects 'markdown' blocks (invalid_blocks error),
        automatically retries with legacy mrkdwn section blocks using the
        original markdown text stored in payload.source_text.
        """
        from typing import Any

        from src.service.bots.slack_bot_service import SlackBotService

        channel_id = reply_context.get("channel_id", "")
        team_id = reply_context.get("team_id", "")
        thinking_ts = reply_context.get("thinking_message_ts", "")
        thread_ts = reply_context.get("thread_ts") or None

        if not channel_id or not team_id:
            logger.error(
                "Missing Slack reply context: channel_id=%s team_id=%s",
                bool(channel_id),
                bool(team_id),
            )
            return False

        bot_service = SlackBotService()
        blocks = payload.content.get("blocks") or []
        text = payload.content.get("text") or payload.text_fallback or ""
        attachments = payload.attachments

        # Check if this payload uses markdown blocks (needs fallback capability)
        has_markdown_blocks = any(b.get("type") == "markdown" for b in blocks)

        # Send using raw methods (which return error codes) when we need fallback
        if has_markdown_blocks:
            # Build the API payload (channel/ts added by raw methods)
            api_payload: dict[str, Any] = {
                "text": bot_service._fallback_text(text),
            }
            if blocks:
                api_payload["blocks"] = blocks
            if attachments:
                api_payload["attachments"] = attachments
            if thread_ts:
                api_payload["thread_ts"] = thread_ts

            if thinking_ts:
                success, error = await bot_service._update_message_raw(
                    team_id,
                    channel_id,
                    thinking_ts,
                    api_payload,
                )
            else:
                success, error = await bot_service._post_message_raw(
                    team_id,
                    channel_id,
                    api_payload,
                )

            if success:
                return True

            # Fallback: if markdown blocks were rejected, retry with legacy mrkdwn
            if error in ("invalid_blocks", "invalid_block"):
                logger.warning(
                    "Slack rejected markdown blocks, retrying with mrkdwn fallback (team=%s)",
                    team_id,
                )
                source_text = payload.source_text
                if source_text:
                    from src.adapters.slack_adapter import SlackAdapter

                    adapter = SlackAdapter()
                    fb_payload = adapter.render_text_fallback(source_text)
                    fb_blocks = fb_payload.content.get("blocks") or []
                    fb_text = fb_payload.content.get("text") or fb_payload.text_fallback or ""

                    fb_api_payload: dict[str, Any] = {
                        "text": bot_service._fallback_text(fb_text),
                        "blocks": fb_blocks,
                    }
                    if thread_ts:
                        fb_api_payload["thread_ts"] = thread_ts

                    if thinking_ts:
                        success, _ = await bot_service._update_message_raw(
                            team_id,
                            channel_id,
                            thinking_ts,
                            fb_api_payload,
                        )
                    else:
                        success, _ = await bot_service._post_message_raw(
                            team_id,
                            channel_id,
                            fb_api_payload,
                        )
                    return success

            return False

        # Non-markdown-block payloads: use existing methods (no fallback needed)
        if thinking_ts:
            if blocks or attachments:
                updated = await bot_service._update_message_blocks(
                    team_id=team_id,
                    channel_id=channel_id,
                    message_ts=thinking_ts,
                    blocks=blocks,
                    text_fallback=text,
                    attachments=attachments,
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
            # Update failed — try to blank the stale thinking message
            logger.warning("chat.update failed, falling back to new message")
            await bot_service._update_message(
                team_id=team_id,
                channel_id=channel_id,
                message_ts=thinking_ts,
                text=" ",
            )

        if blocks or attachments:
            return await bot_service.send_block_kit_message(
                team_id=team_id,
                channel_id=channel_id,
                blocks=blocks,
                text_fallback=text,
                attachments=attachments,
                thread_ts=thread_ts,
            )
        else:
            return await bot_service._send_message(team_id, channel_id, text, thread_ts=thread_ts)
