"""Message processing service for chat worker.

This service handles all business logic for chat message processing:
- Loading job and conversation data
- Executing agent workflows
- Saving results and finalizing jobs

IMPORTANT: This service follows the fresh session pattern. Sessions are acquired
only when needed for database operations and released immediately after.
No sessions are held during long-running operations (LLM calls).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlmodel import col

from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
    ChatMessage,
    ChatMessageAttachmentLink,
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_master_session,
    acquire_org_session,
)
from src.infra.messaging.redis_channels import channel_for_chat_job
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.base.worker_utils import get_organisation_agent_context

logger = get_logger("chat_message_processing_service")

# Frame type constants
FRAME_TYPE_ANSWER_DELTA = "answer.delta"
FRAME_TYPE_ANSWER_REPLACE = "answer.replace"
FRAME_TYPE_ANSWER_METRICS = "answer.metrics"
FRAME_TYPE_SUGGESTIONS_REPLACE = "suggestions.replace"
FRAME_TYPE_SOURCES_REPLACE = "sources.replace"
FRAME_TYPE_QUESTION_PROMPT = "question.prompt"
FRAME_TYPE_PROGRESS_UPDATE = "progress.update"
FRAME_TYPE_TOOL_USAGE = "tool.usage"
FRAME_TYPE_THINKING_UPDATE = "thinking.update"
FRAME_TYPE_FINALIZE = "finalize"
FRAME_TYPE_VISUALIZATION = "visualization"
FRAME_TYPE_STRUCTURED_DATA = "structured_data"
FRAME_TYPE_TASK_LIST = "task_list"
FRAME_TYPE_TOKEN_USAGE = "token.usage"
FRAME_TYPE_ERROR = "error"


@dataclass
class AttachmentInfo:
    """Minimal attachment metadata for media resolution."""

    attachment_id: UUID
    storage_path: str
    mimetype: str
    filename: str


@dataclass
class ConversationData:
    """Data loaded from database for message processing."""

    job_id: UUID
    session_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    chat_type: str
    user_id: UUID | None
    user_message_session_id: UUID
    user_message_parent_id: UUID | None
    user_message_type: ChatMessageType
    user_email: str | None = None
    job_metadata: dict[str, Any] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    attachments: list[AttachmentInfo] = field(default_factory=list)


@dataclass
class AgentResult:
    """Result from agent execution."""

    answer: str | None = None
    confidence: float | None = None
    suggestions: list[str] | None = None
    sources: list[dict] | None = None
    user_questions: list[dict] = field(default_factory=list)
    progress_updates: list[dict] = field(default_factory=list)
    tool_usage: list[dict] = field(default_factory=list)
    thinking_updates: list[dict] = field(default_factory=list)
    visualizations: list[dict] = field(default_factory=list)
    structured_data: list[dict] = field(default_factory=list)
    task_list: list[dict] = field(default_factory=list)
    token_usage: dict[str, Any] | None = None
    model_name: str | None = None
    cost: float | None = None
    duration_seconds: float | None = None


class RedisPublisher:
    """Publish JSON frames to a Redis Stream for a specific job."""

    def __init__(self, organisation_id: UUID, job_id: UUID, maxlen: int = 500):
        self._stream_key = channel_for_chat_job(organisation_id, job_id)
        self._maxlen = maxlen

    def publish(self, frame: dict) -> None:
        """Append a frame best-effort; job persistence remains the durable result."""
        if xadd_json_frame(self._stream_key, frame, maxlen=self._maxlen, approximate=True) is None:
            logger.warning("Failed to XADD frame for job %s", frame.get("job_id"))


class MessageProcessingService:
    """Service for handling chat message processing.

    This service follows the fresh session pattern:
    - Phase 1: Load job and conversation data (acquire session, load, release)
    - Phase 2: Execute agent workflow (NO session held during LLM calls)
    - Phase 3: Save results and finalize job (acquire session, save, release)

    Sessions are acquired using `for session in acquire_org_session()` which
    automatically releases the session when the block exits.
    """

    def __init__(self, organisation_id: UUID):
        """Initialize the service.

        Args:
            organisation_id: The organisation UUID
        """
        self.organisation_id = organisation_id
        self.publisher: RedisPublisher | None = None

    def process(self, job_id: UUID, celery_task=None) -> bool:
        """Process a chat message job.

        This is the main entry point for message processing.

        Args:
            job_id: The job UUID
            celery_task: Optional Celery task instance for retry handling

        Returns:
            True if processing succeeded, False otherwise
        """
        self.publisher = RedisPublisher(self.organisation_id, job_id)

        try:
            # Phase 1: Load job and conversation data (DB operation)
            conversation_data = self._load_job_data(job_id, celery_task)
            if not conversation_data:
                return False

            # Phase 2: Execute agent workflow (NO session held - LLM calls)
            result = self._execute_agent_workflow(conversation_data)

            # Phase 3: Save results and finalize (DB operation)
            self._save_results(conversation_data, result)

            logger.info("Job %s completed successfully", job_id)
            return True

        except Exception as e:
            error_message = str(e)
            logger.exception(
                "Message processing failed org=%s job=%s error=%s",
                self.organisation_id,
                job_id,
                e,
            )
            self._mark_job_failed(job_id, error_message)
            self._publish_failure_frames(job_id, error_message)
            return False

    def _load_job_data(self, job_id: UUID, celery_task=None) -> ConversationData | None:
        """Load job and conversation data from database.

        Acquires session via context manager, loads data, marks job in progress.
        Session is automatically released when the block exits.

        Returns:
            ConversationData if successful, None if job not found or invalid
        """
        for session in acquire_org_session(self.organisation_id):
            # Load and validate job
            job: ChatMessageJob | None = session.get(ChatMessageJob, job_id)
            if not job:
                # Handle race condition - job may not be committed yet
                if celery_task:
                    countdown = min(2**celery_task.request.retries, 30)
                    logger.warning(
                        "Chat job %s not found yet (org=%s). Retrying in %ss.",
                        job_id,
                        self.organisation_id,
                        countdown,
                    )
                    raise celery_task.retry(
                        exc=RuntimeError(f"Job {job_id} not found"),
                        countdown=countdown,
                    )
                logger.warning("Job %s not found", job_id)
                return None

            # Recovery may republish a task after broker acceptance. Lock and
            # re-read the row so only one delivery can claim a pending job.
            session.refresh(job, with_for_update=True)
            if job.status != JobStatus.PENDING:
                logger.info("Skipping already claimed chat job=%s status=%s", job_id, job.status)
                session.rollback()
                return None

            job.status = JobStatus.IN_PROGRESS
            session.add(job)
            session.commit()

            # Extract IDs from job metadata
            md = job.job_metadata or {}
            session_id = md.get("session_id")
            user_message_id = md.get("user_message_id")
            assistant_message_id = md.get("assistant_message_id")

            if not session_id or not user_message_id or not assistant_message_id:
                raise RuntimeError("Missing required IDs in job_metadata")

            try:
                session_id = UUID(str(session_id))
                user_message_id = UUID(str(user_message_id))
                assistant_message_id = UUID(str(assistant_message_id))
            except Exception as e:
                raise RuntimeError("Invalid UUID in job_metadata") from e

            # Load chat session
            chat_session: ChatSession | None = session.get(ChatSession, session_id)
            if not chat_session:
                raise RuntimeError(f"Session {session_id} not found")

            # Load user message
            user_message: ChatMessage | None = session.get(ChatMessage, user_message_id)
            if not user_message:
                raise RuntimeError(f"User message {user_message_id} not found")

            from src.service.chat.chat_attachment_manifest import (
                AttachmentContextItem,
                build_attachment_manifest,
            )

            # Rebuild manifests from canonical attachment links on every run. We do not persist
            # fully expanded file text in chat history because that would bloat prompts and go stale.
            newest_history_records = (
                session.query(ChatMessage)
                .filter(col(ChatMessage.session_id) == session_id)
                .order_by(col(ChatMessage.created_at).desc(), col(ChatMessage.message_id).desc())
                .limit(30)
                .all()
            )
            history_records = list(reversed(newest_history_records))
            history_message_ids = [msg.message_id for msg in history_records]

            stored_attachments_by_message: dict[UUID, list[AttachmentInfo]] = {}
            manifest_items_by_message: dict[UUID, list[AttachmentContextItem]] = {}
            if history_message_ids:
                attachment_rows = (
                    session.query(
                        ChatMessageAttachmentLink.message_id,
                        Attachment.attachment_id,
                        Attachment.storage_path,
                        Attachment.mimetype,
                        Attachment.filename,
                        Attachment.source_provider,
                    )
                    .join(
                        Attachment,
                        Attachment.attachment_id == ChatMessageAttachmentLink.attachment_id,
                    )
                    .filter(
                        ChatMessageAttachmentLink.message_id.in_(history_message_ids),
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
                    stored_attachments_by_message.setdefault(row.message_id, []).append(
                        AttachmentInfo(
                            attachment_id=row.attachment_id,
                            storage_path=row.storage_path,
                            mimetype=row.mimetype,
                            filename=row.filename,
                        )
                    )
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

            history = []
            for msg in history_records:
                history.append(
                    {
                        "message_id": str(msg.message_id),
                        "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                        "content": msg.content,
                        "attachment_manifest": build_attachment_manifest(
                            manifest_items_by_message.get(msg.message_id, [])
                        )
                        or None,
                        "created_at": msg.created_at.isoformat() if msg.created_at else None,
                    }
                )

            attachments = stored_attachments_by_message.get(user_message_id, [])
            # Look up user email from master schema (org role can't access public.users)
            user_email: str | None = None
            if chat_session.user_id:
                from src.domain.models.master_tables import User

                for master_session in acquire_master_session():
                    user_obj: User | None = master_session.get(User, chat_session.user_id)
                    if user_obj:
                        user_email = user_obj.email
                    break

            logger.info(
                "Loaded conversation data for job %s: session=%s history_count=%d",
                job_id,
                session_id,
                len(history),
            )

            return ConversationData(
                job_id=job_id,
                session_id=session_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                chat_type=chat_session.chat_type,
                user_id=chat_session.user_id,
                user_email=user_email,
                job_metadata=md,
                user_message_session_id=user_message.session_id,
                user_message_parent_id=user_message.message_id,
                user_message_type=user_message.message_type,
                history=history,
                attachments=attachments,
            )

        # Should not reach here, but satisfy type checker
        return None

    def _execute_agent_workflow(self, data: ConversationData) -> AgentResult:
        """Execute agent workflow.

        This method does NOT hold any database sessions.
        All LLM operations happen here without DB connections.
        """
        try:
            result_summary = run_async(self._consume_agent_stream(data))

            if result_summary is None:
                logger.error("Agent stream returned None for job %s", data.job_id)
                return self._create_error_result()

            answer = result_summary.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                # COMPLETED is the frontend's terminal success signal; persisting empty
                # text would acknowledge the job without giving the user a response.
                logger.error("Agent stream completed without an answer for job %s", data.job_id)
                answer = self._create_error_result().answer

            return AgentResult(
                answer=answer,
                confidence=result_summary.get("confidence"),
                suggestions=result_summary.get("suggestions"),
                sources=result_summary.get("sources"),
                user_questions=result_summary.get("user_questions") or [],
                progress_updates=result_summary.get("progress_updates") or [],
                tool_usage=result_summary.get("tool_usage") or [],
                thinking_updates=result_summary.get("thinking_updates") or [],
                visualizations=result_summary.get("visualizations") or [],
                structured_data=result_summary.get("structured_data") or [],
                task_list=result_summary.get("task_list") or [],
                token_usage=result_summary.get("token_usage"),
                model_name=result_summary.get("model_name"),
                cost=result_summary.get("cost"),
                duration_seconds=result_summary.get("duration_seconds"),
            )

        except Exception as e:
            logger.exception("Error in agent workflow for job %s: %s", data.job_id, e)
            return self._create_error_result()

    async def _consume_agent_stream(self, data: ConversationData) -> dict:
        """Run the async agent and stream frames.

        Uses fresh session pattern: AgentContext is created without a session.
        Agents and tools acquire sessions on-demand via context.acquire_session().
        """
        from src.agents.context import AgentContext
        from src.agents.orchestrator import run_agent_for_chat

        result_summary: dict[str, Any] = {
            "answer_parts": [],
            "final_answer": None,
            "confidence": None,
            "suggestions": None,
            "sources": None,
            "user_questions": [],
            "progress_updates": [],
            "tool_usage": [],
            "thinking_updates": [],
            "visualizations": [],
            "structured_data": [],
            "task_list": [],
            "token_usage": None,
            "model_name": None,
            "cost": None,
            "duration_seconds": None,
        }

        sequence_counter = 0

        # Look up real org name from master (public) schema
        org_name = "Our Organization"
        organisation_profile: str | None = None
        async for master_session in acquire_master_async_session():
            loaded_name, organisation_profile = await get_organisation_agent_context(
                master_session, self.organisation_id
            )
            org_name = loaded_name or org_name

        # Build context WITHOUT a session
        context = AgentContext(
            organisation_id=self.organisation_id,
            org_name=org_name,
            organisation_profile=organisation_profile,
            user_id=data.user_id,
            user_email=data.user_email,
            message_id=str(data.assistant_message_id),
            metadata={
                # Security boundary for attachment reads: the tool can only open files that
                # belong to this session or the explicitly allowed current-turn attachment IDs.
                "chat_session_id": str(data.session_id),
                "current_user_message_id": str(data.user_message_id),
                "allowed_attachment_ids": [
                    str(attachment.attachment_id) for attachment in data.attachments
                ],
                **(
                    {"user_timezone": str(data.job_metadata["user_timezone"])}
                    if data.job_metadata.get("user_timezone")
                    else {}
                ),
                **(
                    {"current_datetime": str(data.job_metadata["current_datetime"])}
                    if data.job_metadata.get("current_datetime")
                    else {}
                ),
            },
        )

        async for frame in run_agent_for_chat(
            chat_type=data.chat_type,
            messages=data.history,
            context=context,
            images=None,
            attachment_text=None,
        ):
            try:
                frame_data = frame.model_dump()
            except Exception:
                # Bind frame as default parameter to avoid loop variable capture issue
                frame_data = getattr(frame, "dict", lambda f=frame: dict(f))()

            # Transform to production-level frame structure
            production_frame = {
                **frame_data,
                "job_id": str(data.job_id),
                "assistant_message_id": str(data.assistant_message_id),
                "version": "1.0",
                "timestamp": time.time(),
                "sequence": sequence_counter,
            }

            sequence_counter += 1

            # Process frame data for summary (before publish — captures internal-only frames)
            self._process_frame_data(frame_data, result_summary)

            # Publish frame to Redis Stream (skip internal-only frames)
            if self.publisher and frame_data.get("type") != FRAME_TYPE_TOKEN_USAGE:
                self.publisher.publish(production_frame)

        return {
            "answer": (
                "".join(result_summary["answer_parts"])
                if result_summary["answer_parts"]
                else result_summary["final_answer"]
            ),
            "confidence": result_summary["confidence"],
            "suggestions": result_summary["suggestions"],
            "sources": result_summary["sources"],
            "user_questions": result_summary["user_questions"],
            "progress_updates": result_summary["progress_updates"],
            "tool_usage": result_summary["tool_usage"],
            "thinking_updates": result_summary["thinking_updates"],
            "visualizations": result_summary["visualizations"],
            "structured_data": result_summary["structured_data"],
            "task_list": result_summary["task_list"],
            "token_usage": result_summary["token_usage"],
            "model_name": result_summary["model_name"],
            "cost": result_summary["cost"],
            "duration_seconds": result_summary["duration_seconds"],
        }

    def _process_frame_data(self, data: dict, result_summary: dict) -> None:
        """Process individual frame data and update result summary."""
        ftype = data.get("type")

        if ftype in [FRAME_TYPE_ANSWER_DELTA, FRAME_TYPE_ANSWER_REPLACE]:
            if ftype == FRAME_TYPE_ANSWER_DELTA:
                result_summary["answer_parts"].append(data.get("content") or "")
            else:
                result_summary["final_answer"] = data.get("content") or ""
        elif ftype == FRAME_TYPE_ANSWER_METRICS:
            result_summary["confidence"] = data.get("confidence")
            if not result_summary["sources"] and data.get("citations"):
                result_summary["sources"] = data.get("citations")
        elif ftype == FRAME_TYPE_SUGGESTIONS_REPLACE:
            items = data.get("items") or []
            suggestions = []
            for i in items:
                if isinstance(i, str):
                    suggestions.append(i)
                elif isinstance(i, dict):
                    label = i.get("label") or i.get("text") or i.get("title")
                    if label:
                        suggestions.append(str(label))
            result_summary["suggestions"] = suggestions or None
        elif ftype == FRAME_TYPE_SOURCES_REPLACE:
            result_summary["sources"] = data.get("sources")
        elif ftype == FRAME_TYPE_QUESTION_PROMPT:
            result_summary["user_questions"].append(
                {
                    "prompt": data.get("prompt"),
                    "fields": data.get("fields"),
                    "await_user_response": data.get("await_user_response", True),
                }
            )
        elif ftype == FRAME_TYPE_PROGRESS_UPDATE:
            result_summary["progress_updates"].append(
                {
                    "stage": data.get("stage"),
                    "message": data.get("message"),
                    "progress": data.get("progress"),
                    "eta_seconds": data.get("eta_seconds"),
                }
            )
        elif ftype == FRAME_TYPE_TOOL_USAGE:
            result_summary["tool_usage"].append(
                {
                    "tool_name": data.get("tool_name"),
                    "status": data.get("status"),
                    "duration_ms": data.get("duration_ms"),
                    "result_summary": data.get("result_summary"),
                }
            )
        elif ftype == FRAME_TYPE_THINKING_UPDATE:
            result_summary["thinking_updates"].append(
                {
                    "thought": data.get("thought"),
                    "stage": data.get("stage"),
                }
            )
        elif ftype == FRAME_TYPE_VISUALIZATION:
            result_summary["visualizations"].append(
                {
                    "visualization_type": data.get("visualization_type"),
                    "diagram_type": data.get("diagram_type"),
                    "content": data.get("content"),
                    "title": data.get("title"),
                    "description": data.get("description"),
                }
            )
        elif ftype == FRAME_TYPE_STRUCTURED_DATA:
            result_summary["structured_data"].append(
                {
                    "data_type": data.get("data_type"),
                    "title": data.get("title"),
                    "columns": data.get("columns"),
                    "rows": data.get("rows"),
                    "items": data.get("items"),
                    "metadata": data.get("metadata"),
                }
            )
        elif ftype == FRAME_TYPE_TASK_LIST:
            tasks = data.get("tasks") or []
            result_summary["task_list"] = [
                {"id": task.get("id"), "task": task.get("task"), "status": task.get("status")}
                for task in tasks
                if isinstance(task, dict)
            ]
        elif ftype == FRAME_TYPE_TOKEN_USAGE:
            result_summary["token_usage"] = {
                "input_tokens": data.get("input_tokens", 0),
                "output_tokens": data.get("output_tokens", 0),
                "total_tokens": data.get("total_tokens", 0),
                "reasoning_tokens": data.get("reasoning_tokens", 0),
                "cache_read_tokens": data.get("cache_read_tokens", 0),
                "cache_write_tokens": data.get("cache_write_tokens", 0),
            }
            result_summary["model_name"] = data.get("model_name")
            result_summary["cost"] = data.get("cost")
            result_summary["duration_seconds"] = data.get("duration_seconds")
            logger.info(
                "Captured token.usage frame: model=%s total_tokens=%s cost=%s duration=%s",
                result_summary["model_name"],
                result_summary["token_usage"].get("total_tokens"),
                result_summary["cost"],
                result_summary["duration_seconds"],
            )

    def _save_results(self, data: ConversationData, result: AgentResult) -> None:
        """Save agent results and finalize job.

        Acquires session via context manager, saves data.
        Session is automatically released when the block exits.
        """
        for session in acquire_org_session(self.organisation_id):
            # Persist the assistant row and terminal job state in the same
            # session so clients never observe completion without its answer.
            assistant_message = ChatMessage(
                message_id=data.assistant_message_id,
                session_id=data.user_message_session_id,
                parent_message_id=data.user_message_parent_id,
                role=ChatMessageRole.ASSISTANT,
                message_type=data.user_message_type,
                content={"text": result.answer or ""},
                model_name=result.model_name,
                extra={
                    "confidence_score": result.confidence,
                    "has_user_questions": bool(result.user_questions),
                    "progress_tracked": bool(result.progress_updates),
                    "tool_usage_count": len(result.tool_usage),
                    "thinking_updates_count": len(result.thinking_updates),
                    "visualizations_count": len(result.visualizations),
                    "structured_data_count": len(result.structured_data),
                    "task_list_count": len(result.task_list),
                },
                agent_data={
                    "sources": result.sources,
                    "suggested_questions": result.suggestions or [],
                    "user_questions": result.user_questions,
                    "progress_updates": result.progress_updates,
                    "tool_usage": result.tool_usage,
                    "thinking_updates": result.thinking_updates,
                    "visualizations": result.visualizations,
                    "structured_data": result.structured_data,
                    "task_list": result.task_list,
                },
            )
            session.add(assistant_message)

            # Finalize job
            job = session.get(ChatMessageJob, data.job_id)
            if job:
                job.result_data = {
                    "answer": result.answer,
                    "confidence": result.confidence,
                    "sources": result.sources,
                    "suggested_questions": result.suggestions,
                    "assistant_message_id": str(data.assistant_message_id),
                    "user_questions_count": len(result.user_questions),
                    "progress_updates_count": len(result.progress_updates),
                    "tool_usage_count": len(result.tool_usage),
                    "thinking_updates_count": len(result.thinking_updates),
                    "visualizations_count": len(result.visualizations),
                    "structured_data_count": len(result.structured_data),
                    "task_list": result.task_list,
                    "token_usage": result.token_usage,
                    "model_name": result.model_name,
                }
                job.token_usage = result.token_usage
                job.model_name = result.model_name
                if result.cost is not None:
                    from decimal import Decimal

                    job.cost = Decimal(str(result.cost))
                if result.duration_seconds is not None:
                    job.latency_ms = int(result.duration_seconds * 1000)
                if not result.token_usage:
                    logger.warning(
                        "No token_usage on AgentResult for job %s — columns will be NULL",
                        data.job_id,
                    )
                if data.user_id:
                    job.user_id = data.user_id
                logger.info(
                    "Saving job %s: token_usage=%s, model_name=%s, cost=%s, latency_ms=%s, user_id=%s",
                    data.job_id,
                    job.token_usage,
                    job.model_name,
                    job.cost,
                    job.latency_ms,
                    job.user_id,
                )
                job.status = JobStatus.COMPLETED
                session.add(job)

            session.commit()

            logger.info(
                "Saved assistant message %s with answer (length=%d)",
                data.assistant_message_id,
                len(result.answer or ""),
            )

        # Publish final frames (outside of session)
        self._publish_final_frames(data, result)

    def _publish_final_frames(self, data: ConversationData, result: AgentResult) -> None:
        """Publish final frames to Redis."""
        if not self.publisher:
            return

        base_frame = {
            "job_id": str(data.job_id),
            "assistant_message_id": str(data.assistant_message_id),
            "version": "1.0",
            "timestamp": time.time(),
        }

        if result.answer:
            self.publisher.publish(
                {
                    **base_frame,
                    "type": FRAME_TYPE_ANSWER_REPLACE,
                    "content": result.answer,
                    "format": "markdown",
                }
            )

        if result.confidence is not None:
            self.publisher.publish(
                {
                    **base_frame,
                    "type": FRAME_TYPE_ANSWER_METRICS,
                    "confidence": result.confidence,
                }
            )

        if result.suggestions:
            self.publisher.publish(
                {
                    **base_frame,
                    "type": FRAME_TYPE_SUGGESTIONS_REPLACE,
                    "items": result.suggestions,
                }
            )

        # Finalize frame
        self.publisher.publish(
            {
                **base_frame,
                "type": FRAME_TYPE_FINALIZE,
                "reason": "done",
            }
        )

    def _publish_failure_frames(self, job_id: UUID, error_message: str) -> None:
        """Publish the terminal failure so a connected SSE client can stop waiting."""
        if not self.publisher:
            return
        base_frame = {
            "job_id": str(job_id),
            "version": "1.0",
            "timestamp": time.time(),
        }
        self.publisher.publish(
            {
                **base_frame,
                "type": FRAME_TYPE_ERROR,
                "code": "chat_processing_failed",
                "message": error_message,
            }
        )
        self.publisher.publish(
            {
                **base_frame,
                "type": FRAME_TYPE_FINALIZE,
                "reason": "error",
            }
        )

    def _mark_job_failed(self, job_id: UUID, error_message: str) -> None:
        """Mark job as failed.

        Acquires session via context manager, updates status.
        Session is automatically released when the block exits.
        """
        try:
            for session in acquire_org_session(self.organisation_id):
                job = session.get(ChatMessageJob, job_id)
                if job:
                    job.status = JobStatus.FAILED
                    job.error_message = error_message[:500]
                    session.add(job)
                    session.commit()
        except Exception:
            logger.debug("Failed to persist FAILED state for job %s", job_id, exc_info=True)

    def _create_error_result(self) -> AgentResult:
        """Create error result."""
        return AgentResult(
            answer="I apologize, but I encountered an error processing your request.",
        )

    async def _resolve_attachments(self, attachments: list[AttachmentInfo]) -> list:
        """Prepare current-turn image attachments for native vision input."""
        if not attachments:
            return []

        from agno.media import Image as AgnoImage

        from src.infra.media.chat_attachment_text import is_image_mimetype
        from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

        images: list[AgnoImage] = []
        image_attachments = [
            attachment for attachment in attachments if is_image_mimetype(attachment.mimetype)
        ]
        if not image_attachments:
            return images

        storage = AzureOrganisationBlobStorage(organisation_id=self.organisation_id)
        try:
            for attachment in image_attachments:
                if not attachment.storage_path:
                    logger.warning(
                        "Skipping image attachment %s: no storage_path",
                        attachment.attachment_id,
                    )
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
                        "Failed to load image attachment %s (%s), skipping",
                        attachment.attachment_id,
                        attachment.filename,
                        exc_info=True,
                    )
        finally:
            await storage.close()

        logger.info("Attachment resolution: %d native images", len(images))
        return images

    @staticmethod
    def _extract_text(content: bytes, mimetype: str, filename: str) -> str:
        """Extract text from document bytes based on mimetype."""
        import io

        if mimetype == "application/pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            pages = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)

        if mimetype == ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
            from docx import Document

            doc = Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs)

        if mimetype == (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ):
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE

            prs = Presentation(io.BytesIO(content))
            parts: list[str] = []
            for slide_num, slide in enumerate(prs.slides, 1):
                slide_parts: list[str] = [f"--- Slide {slide_num} ---"]
                for shape in slide.shapes:
                    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                        for child in shape.shapes:
                            if hasattr(child, "text") and child.text.strip():
                                slide_parts.append(child.text.strip())
                    elif shape.has_table:
                        for row in shape.table.rows:
                            row_text = "\t".join(c.text.strip() for c in row.cells)
                            if row_text.strip():
                                slide_parts.append(row_text)
                    elif hasattr(shape, "text") and shape.text.strip():
                        slide_parts.append(shape.text.strip())
                if slide.has_notes_slide:
                    ntf = slide.notes_slide.notes_text_frame
                    if ntf is not None and ntf.text.strip():
                        slide_parts.append(f"[Notes] {ntf.text.strip()}")
                if len(slide_parts) > 1:
                    parts.extend(slide_parts)
            return "\n".join(parts)

        if mimetype == ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            parts_xl: list[str] = []
            try:
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    sheet_rows: list[str] = []
                    for row in ws.iter_rows(values_only=True):
                        vals = [str(v).strip() if v is not None else "" for v in row]
                        if any(vals):
                            sheet_rows.append("\t".join(vals))
                    if sheet_rows:
                        parts_xl.append(f"--- Sheet: {sheet_name} ---")
                        parts_xl.extend(sheet_rows)
            finally:
                wb.close()
            return "\n".join(parts_xl)

        if mimetype.startswith("text/") or mimetype in (
            "application/json",
            "application/xml",
            "application/x-ndjson",
            "application/ndjson",
            "application/jsonl",
        ):
            return content.decode("utf-8", errors="replace")

        logger.warning("No text extractor for mimetype %s (%s)", mimetype, filename)
        return ""
