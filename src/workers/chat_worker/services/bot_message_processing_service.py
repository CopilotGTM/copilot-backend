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

            bot_context = dict(bot_context or {})
            conversation_type = bot_context.get("conversation_type", "personal")

            # Slack voice notes are transcribed on the worker so webhook handling stays fast.
            message_text, bot_context = run_async(
                self._prepare_bot_message_input(
                    message_text=message_text,
                    provider=provider,
                    reply_context=reply_context,
                    bot_context=bot_context,
                    job_id=job_id,
                )
            )

            # For personal chats, session + user message were created at dispatch
            # time by the bot service. Load history from that session.
            session_id: UUID | None = None
            if job_id and conversation_type == "personal":
                session_id = self._get_session_from_job(job_id)
                if session_id:
                    chat_history = self._load_history(session_id)
                    # Rehydrate these IDs on the worker so attachment tools can enforce
                    # session-scoped access during follow-up questions.
                    bot_context["chat_session_id"] = str(session_id)
                    user_message_id = self._get_user_message_from_job(job_id)
                    if user_message_id:
                        bot_context["current_user_message_id"] = str(user_message_id)

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

    def _persist_user_message_text(self, job_id: UUID, text: str) -> None:
        """Backfill the persisted personal-chat user message after worker-side transcription."""
        from src.domain.models.organisation_tables.chat_message_models import ChatMessage
        from src.domain.models.organisation_tables.job_models import ChatMessageJob
        from src.infra.database.sessions import org_session_scope

        with org_session_scope(self.organisation_id) as session:
            job = session.get(ChatMessageJob, job_id)
            if not job or not job.message_id:
                return

            message = session.get(ChatMessage, job.message_id)
            if not message:
                return

            content = dict(message.content or {})
            content["text"] = text
            message.content = content

            extra = dict(message.extra or {})
            extra["input_modality"] = "audio"
            extra["audio_origin"] = "slack_audio"
            message.extra = extra
            session.add(message)
            session.commit()

    def _load_history(self, session_id: UUID, limit: int = 20) -> list[dict] | None:
        """Load recent chat history from the DB for this session.

        Returns messages in chronological order (oldest first).
        Strips the last user message (the current one just persisted)
        since _build_messages adds it separately.
        """
        from sqlmodel import col, select

        from src.domain.models.organisation_tables.chat_message_models import (
            Attachment,
            AttachmentStatus,
            ChatMessage,
            ChatMessageAttachmentLink,
        )
        from src.infra.database.sessions import org_session_scope
        from src.service.chat.chat_attachment_manifest import (
            AttachmentContextItem,
            build_attachment_manifest,
        )

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

            messages = list(reversed(rows))
            if messages and getattr(messages[-1].role, "value", messages[-1].role) == "user":
                messages = messages[:-1]

            if not messages:
                return None

            # Rebuild manifests from linked attachments on every load so personal bot chats
            # behave like web chat and can answer follow-up questions about older files.
            manifest_items_by_message: dict[UUID, list[AttachmentContextItem]] = {}
            message_ids = [message.message_id for message in messages]
            if message_ids:
                attachment_rows = (
                    session.query(
                        ChatMessageAttachmentLink.message_id,
                        Attachment.attachment_id,
                        Attachment.filename,
                        Attachment.mimetype,
                        Attachment.source_provider,
                    )
                    .join(
                        Attachment,
                        Attachment.attachment_id == ChatMessageAttachmentLink.attachment_id,
                    )
                    .filter(
                        ChatMessageAttachmentLink.message_id.in_(message_ids),
                        Attachment.organisation_id == self.organisation_id,
                        Attachment.is_deleted.is_(False),
                        Attachment.status.notin_(
                            [
                                AttachmentStatus.PENDING_UPLOAD.value,
                                AttachmentStatus.FAILED.value,
                            ]
                        ),
                    )
                    .order_by(
                        ChatMessageAttachmentLink.message_id,
                        ChatMessageAttachmentLink.attachment_order,
                    )
                    .all()
                )
                for row in attachment_rows:
                    manifest_items_by_message.setdefault(row.message_id, []).append(
                        AttachmentContextItem(
                            attachment_id=str(row.attachment_id),
                            filename=row.filename,
                            mimetype=row.mimetype,
                            source_provider=(
                                getattr(row.source_provider, "value", row.source_provider)
                                if row.source_provider
                                else None
                            ),
                        )
                    )

        return [
            {
                "message_id": str(msg.message_id),
                "role": getattr(msg.role, "value", msg.role),
                "content": msg.content,
                "attachment_manifest": build_attachment_manifest(
                    manifest_items_by_message.get(msg.message_id, [])
                )
                or None,
            }
            for msg in messages
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

    @staticmethod
    def _merge_audio_transcript_text(text: str, transcript: str) -> str:
        """Preserve typed context while using the transcript as the actual user input."""
        transcript_text = transcript.strip()
        if not text.strip():
            return transcript_text
        return f"{text.strip()}\n\n[Slack audio transcript]\n{transcript_text}"

    async def _prepare_bot_message_input(
        self,
        message_text: str,
        provider: str,
        reply_context: dict,
        bot_context: dict,
        job_id: UUID | None,
    ) -> tuple[str, dict]:
        """Resolve provider-specific transient inputs before the agent sees the message."""
        if provider != "slack":
            return message_text, bot_context

        slack_audio_files = list(bot_context.get("slack_audio_files") or [])
        if not slack_audio_files:
            return message_text, bot_context

        team_id = str(reply_context.get("team_id") or "")
        if not team_id:
            logger.warning("Skipping Slack audio transcription: missing team_id in reply context")
            return message_text, bot_context

        transcript = await self._transcribe_slack_audio_files(slack_audio_files, team_id=team_id)
        if not transcript:
            return message_text, bot_context

        merged_text = self._merge_audio_transcript_text(message_text, transcript)
        bot_context = dict(bot_context)
        bot_context["input_modality"] = "audio"
        bot_context["audio_origin"] = "slack_audio"

        if job_id and bot_context.get("conversation_type") == "personal":
            self._persist_user_message_text(job_id, merged_text)

        return merged_text, bot_context

    async def _transcribe_slack_audio_files(
        self,
        slack_files: list[dict],
        *,
        team_id: str,
    ) -> str:
        """Download and transcribe Slack voice recordings on the chat worker."""
        import aiohttp

        from src.infra.config.configs import chat_file_upload_config
        from src.infra.media.audio_transcription import transcribe_audio

        bot_token = await self._get_slack_bot_token(team_id)
        if not bot_token:
            logger.warning(
                "Skipping Slack audio transcription for team=%s: bot token unavailable", team_id
            )
            return ""

        timeout = aiohttp.ClientTimeout(
            total=chat_file_upload_config.BOT_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS
        )
        transcripts: list[tuple[str, str]] = []

        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            for file_info in slack_files:
                url = str(file_info.get("url_private_download") or "")
                if not url:
                    continue

                filename = str(file_info.get("name") or "audio.m4a")
                size = int(file_info.get("size") or 0)
                if size and size > chat_file_upload_config.BOT_ATTACHMENT_MAX_INGEST_BYTES:
                    logger.warning(
                        "Skipping Slack audio %s: size=%s exceeds ingest limit",
                        filename,
                        size,
                    )
                    continue

                try:
                    async with http_session.get(
                        url,
                        headers={"Authorization": f"Bearer {bot_token}"},
                    ) as response:
                        response.raise_for_status()
                        content = await response.read()
                except Exception:
                    logger.warning(
                        "Failed to download Slack audio %s for transcription",
                        filename,
                        exc_info=True,
                    )
                    continue

                transcript = (await transcribe_audio(content, filename)).strip()
                if transcript:
                    transcripts.append((filename, transcript))

        if not transcripts:
            return ""
        if len(transcripts) == 1:
            return transcripts[0][1]
        return "\n\n".join(
            f"[Slack audio: {filename}]\n{transcript}" for filename, transcript in transcripts
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
        current_attachments = list((bot_context or {}).get("current_attachments") or [])
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

        if reply_context:
            metadata["service_url"] = reply_context.get("service_url", "")
            metadata["conversation_id"] = reply_context.get("conversation_id", "")
            metadata["channel_id"] = reply_context.get("channel_id", "")
            metadata["team_id"] = reply_context.get("team_id", "")

        user_tz = (bot_context or {}).get("user_timezone", "")
        if user_tz:
            metadata["user_timezone"] = user_tz
        current_datetime = (bot_context or {}).get("current_datetime", "")
        if current_datetime:
            metadata["current_datetime"] = current_datetime
        if bot_context and bot_context.get("chat_session_id"):
            metadata["chat_session_id"] = bot_context.get("chat_session_id")
        if bot_context and bot_context.get("current_user_message_id"):
            metadata["current_user_message_id"] = bot_context.get("current_user_message_id")
        # Group/channel attachments may exist only for the current run, so expose their IDs
        # explicitly instead of pretending they belong to persisted chat history.
        allowed_attachment_ids = [
            str(attachment.get("attachment_id"))
            for attachment in current_attachments
            if attachment.get("attachment_id")
        ]
        if allowed_attachment_ids:
            metadata["allowed_attachment_ids"] = allowed_attachment_ids

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

        images = await self._resolve_ingested_attachment_images(current_attachments)

        # For live status updates on the thinking indicator
        last_status_time = 0.0
        status_ctx = self._build_status_context(channel, reply_context or {})

        try:
            async for frame in run_agent_for_chat(
                chat_type="GENERAL_CHAT",
                messages=messages,
                context=context,
                images=images or None,
                attachment_text=None,
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

    async def _resolve_ingested_attachment_images(
        self, attachment_refs: list[dict[str, str]]
    ) -> list:
        """Load current-turn ingested images for native vision input."""
        if not attachment_refs:
            return []

        from agno.media import Image as AgnoImage
        from sqlmodel import select

        from src.domain.models.organisation_tables.chat_message_models import Attachment
        from src.infra.database.sessions import acquire_org_async_session
        from src.infra.media.chat_attachment_text import is_image_mimetype
        from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

        attachment_ids: list[UUID] = []
        for ref in attachment_refs:
            raw_id = ref.get("attachment_id")
            if not raw_id:
                continue
            try:
                attachment_ids.append(UUID(str(raw_id)))
            except Exception:
                logger.warning("Invalid attachment id in bot context: %s", raw_id)

        if not attachment_ids:
            return []

        attachments: list[Attachment] = []
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(Attachment).where(
                Attachment.attachment_id.in_(attachment_ids),
                Attachment.organisation_id == self.organisation_id,
                Attachment.is_deleted.is_(False),
            )
            attachments = list((await session.execute(stmt)).scalars().all())
            break

        image_attachments = [
            attachment for attachment in attachments if is_image_mimetype(attachment.mimetype or "")
        ]
        if not image_attachments:
            return []

        images: list[AgnoImage] = []
        storage = AzureOrganisationBlobStorage(self.organisation_id)
        try:
            for attachment in image_attachments:
                if not attachment.storage_path:
                    continue
                try:
                    content = await storage.get_blob_content(attachment.storage_path)
                    images.append(
                        AgnoImage(
                            content=content,
                            format=(attachment.mimetype or "image/png").split("/")[-1],
                        )
                    )
                except Exception:
                    logger.warning(
                        "Failed to load ingested attachment image %s",
                        attachment.attachment_id,
                        exc_info=True,
                    )
        finally:
            await storage.close()

        return images

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
        from src.service.chat.chat_attachment_manifest import build_attachment_manifest

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

        # Attach the current-turn manifest to the specific user message so the agent knows
        # which files were present without probing storage first.
        current_attachment_manifest = build_attachment_manifest(
            (bot_context or {}).get("current_attachments") or []
        )

        messages.append(
            {
                "role": "user",
                "content": message_text,
                "attachment_manifest": current_attachment_manifest or None,
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

    async def _get_slack_bot_token(self, team_id: str) -> str | None:
        """Look up the Slack bot token for a workspace."""
        try:
            from src.domain.models.master_tables.bot_platform_installation_models import (
                BotPlatformProvider,
            )
            from src.service.bots.bot_installation_service import BotInstallationService

            service = BotInstallationService()
            async for session in acquire_master_async_session():
                return await service.get_bot_token(session, BotPlatformProvider.SLACK, team_id)
        except Exception:
            logger.warning("Failed to get Slack bot token for team=%s", team_id, exc_info=True)
        return None

    async def _resolve_slack_files(
        self, slack_files: list[dict], bot_token: str
    ) -> tuple[list, str]:
        """Download Slack files and resolve into media objects / extracted text.

        Images → Agno Image objects (native vision).
        Documents → text extracted via pypdf/docx (same as web chat).
        Audio → transcribed via gpt-4o-transcribe, returned as text.
        """
        import aiohttp
        from agno.media import Image as AgnoImage

        from src.infra.config.configs import chat_file_upload_config
        from src.infra.media.audio_transcription import transcribe_audio
        from src.workers.chat_worker.services.message_processing_service import (
            MessageProcessingService,
        )

        image_mimetypes = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
        audio_mimetypes = {
            "audio/mpeg",
            "audio/mp3",
            "audio/wav",
            "audio/ogg",
            "audio/webm",
            "audio/flac",
            "audio/aac",
            "audio/m4a",
            "audio/mp4",
            "audio/x-m4a",
        }
        max_size = chat_file_upload_config.MAX_FILE_SIZE_BYTES

        images: list[AgnoImage] = []
        text_parts: list[str] = []

        async with aiohttp.ClientSession() as http_session:
            for file_info in slack_files:
                name = file_info.get("name", "file")
                mt = (file_info.get("mimetype") or "").lower()
                size = file_info.get("size", 0)
                url = file_info.get("url_private_download", "")

                if not url:
                    continue

                if size > max_size:
                    logger.warning("Slack file %s too large (%d bytes), skipping", name, size)
                    continue

                try:
                    async with http_session.get(
                        url,
                        headers={"Authorization": f"Bearer {bot_token}"},
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        resp.raise_for_status()
                        resp_ct = (resp.content_type or "").lower()
                        content = await resp.read()

                    # Slack returns 200 + HTML login page when auth fails
                    if resp_ct.startswith("text/html"):
                        logger.error(
                            "Slack file download for %s returned HTML instead of file. "
                            "Bot token may lack files:read scope or is invalid. "
                            "Response starts with: %s",
                            name,
                            content[:100].decode("utf-8", errors="replace"),
                        )
                        continue

                    if mt in image_mimetypes:
                        images.append(AgnoImage(content=content, format=mt.split("/")[-1]))
                    elif mt in audio_mimetypes:
                        transcript = await transcribe_audio(content, name)
                        if transcript:
                            text_parts.append(f"--- Audio transcript: {name} ---\n{transcript}")
                    else:
                        extracted = MessageProcessingService._extract_text(content, mt, name)
                        if extracted:
                            text_parts.append(f"--- Attached file: {name} ---\n{extracted}")

                    logger.info("Resolved Slack file %s (%s, %d bytes)", name, mt, len(content))

                except Exception:
                    logger.warning(
                        "Failed to download Slack file %s, skipping", name, exc_info=True
                    )

        attachment_text = "\n\n".join(text_parts)
        logger.info(
            "Slack file resolution: %d images, %d chars text",
            len(images),
            len(attachment_text),
        )
        return images, attachment_text

    async def _get_teams_token(
        self, scope: str = "https://api.botframework.com/.default"
    ) -> str | None:
        """Get an Azure AD token using bot credentials for the given scope.

        Scopes:
        - "https://api.botframework.com/.default" → Bot Framework (inline images)
        - "https://graph.microsoft.com/.default"  → Graph API (OneDrive/SharePoint files)
        """
        try:
            from src.infra.config.configs import teams_config

            app_id, app_secret, tenant_id = await teams_config.get_bot_credentials()
            if not app_id or not app_secret:
                return None

            import aiohttp

            tenant = tenant_id or "botframework.com"
            token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_secret,
                "scope": scope,
            }
            async with (
                aiohttp.ClientSession() as session,
                session.post(token_url, data=data) as resp,
            ):
                if resp.status == 200:
                    result = await resp.json()
                    token = result.get("access_token")
                    return token if isinstance(token, str) else None
                else:
                    logger.warning(
                        "Failed to get Teams token (scope=%s): status=%s",
                        scope,
                        resp.status,
                    )
                    return None
        except Exception:
            logger.warning("Failed to get Teams token", exc_info=True)
            return None

    async def _resolve_teams_files(self, teams_files: list[dict]) -> tuple[list, str]:
        """Download Teams files and resolve into media objects / extracted text.

        Three sources:
        - "upload": pre-authenticated downloadUrl, no auth needed.
        - "onedrive": SharePoint/OneDrive URL, requires Graph API token.
        - "inline": pasted image, requires Bot Framework token.
        """
        import aiohttp
        from agno.media import Image as AgnoImage

        from src.infra.config.configs import chat_file_upload_config
        from src.infra.media.audio_transcription import transcribe_audio
        from src.workers.chat_worker.services.message_processing_service import (
            MessageProcessingService,
        )

        image_mimetypes = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
        audio_mimetypes = {
            "audio/mpeg",
            "audio/mp3",
            "audio/wav",
            "audio/ogg",
            "audio/webm",
            "audio/flac",
            "audio/aac",
            "audio/m4a",
            "audio/mp4",
            "audio/x-m4a",
        }
        max_size = chat_file_upload_config.MAX_FILE_SIZE_BYTES

        # Pre-fetch tokens only for sources that need them
        sources = {f.get("source") for f in teams_files}
        bot_token: str | None = None
        graph_token: str | None = None
        if "inline" in sources:
            bot_token = await self._get_teams_token("https://api.botframework.com/.default")
        if "onedrive" in sources:
            graph_token = await self._get_teams_token("https://graph.microsoft.com/.default")

        images: list[AgnoImage] = []
        text_parts: list[str] = []

        async with aiohttp.ClientSession() as http_session:
            for file_info in teams_files:
                name = file_info.get("name", "file")
                download_url = file_info.get("download_url", "")
                source = file_info.get("source", "upload")

                if not download_url:
                    continue

                # Resolve the actual download URL and auth headers per source
                actual_url, headers = self._resolve_download_params(
                    source, download_url, bot_token, graph_token
                )
                if not actual_url:
                    logger.warning("Skipping Teams file %s: no token for source=%s", name, source)
                    continue

                try:
                    async with http_session.get(
                        actual_url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                        allow_redirects=True,
                    ) as resp:
                        resp.raise_for_status()
                        content = await resp.read()

                    size = len(content)
                    if size > max_size:
                        logger.warning("Teams file %s too large (%d bytes), skipping", name, size)
                        continue

                    # Determine mimetype: use explicit mimetype if present (inline images),
                    # otherwise infer from file extension or response content-type
                    mt = (file_info.get("mimetype") or "").lower()
                    if not mt:
                        mt = self._guess_mimetype(name, file_info.get("file_type", ""))

                    if mt in image_mimetypes:
                        images.append(AgnoImage(content=content, format=mt.split("/")[-1]))
                    elif mt in audio_mimetypes:
                        transcript = await transcribe_audio(content, name)
                        if transcript:
                            text_parts.append(f"--- Audio transcript: {name} ---\n{transcript}")
                    else:
                        extracted = MessageProcessingService._extract_text(content, mt, name)
                        if extracted:
                            text_parts.append(f"--- Attached file: {name} ---\n{extracted}")

                    logger.info(
                        "Resolved Teams file %s (source=%s, %s, %d bytes)",
                        name,
                        source,
                        mt,
                        size,
                    )

                except Exception:
                    logger.warning(
                        "Failed to download Teams file %s (source=%s), skipping",
                        name,
                        source,
                        exc_info=True,
                    )

        attachment_text = "\n\n".join(text_parts)
        logger.info(
            "Teams file resolution: %d images, %d chars text",
            len(images),
            len(attachment_text),
        )
        return images, attachment_text

    @staticmethod
    def _resolve_download_params(
        source: str,
        download_url: str,
        bot_token: str | None,
        graph_token: str | None,
    ) -> tuple[str | None, dict[str, str]]:
        """Build the actual download URL and auth headers for a Teams file.

        For OneDrive files, converts the SharePoint URL into a Graph API
        sharing endpoint that returns the file content directly.
        """
        import base64

        if source == "upload":
            # Pre-authenticated URL, no headers needed
            return download_url, {}

        if source == "inline":
            if not bot_token:
                return None, {}
            return download_url, {"Authorization": f"Bearer {bot_token}"}

        if source == "onedrive":
            if not graph_token:
                return None, {}
            # Encode the SharePoint URL as a Graph sharing token:
            # base64url("u!" + url) → GET /shares/{token}/driveItem/content
            encoded = base64.urlsafe_b64encode(download_url.encode()).decode().rstrip("=")
            share_token = f"u!{encoded}"
            graph_url = f"https://graph.microsoft.com/v1.0/shares/{share_token}/driveItem/content"
            return graph_url, {"Authorization": f"Bearer {graph_token}"}

        return download_url, {}

    @staticmethod
    def _guess_mimetype(filename: str, file_type: str) -> str:
        """Guess mimetype from filename extension or Teams fileType field."""
        import mimetypes

        mt, _ = mimetypes.guess_type(filename)
        if mt:
            return mt.lower()

        # Fallback to Teams fileType field (e.g. "pdf", "docx")
        ext_map = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "txt": "text/plain",
            "csv": "text/csv",
            "json": "application/json",
            "xml": "application/xml",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "ogg": "audio/ogg",
            "m4a": "audio/mp4",
            "webm": "audio/webm",
        }
        return ext_map.get(file_type.lower(), "application/octet-stream")
