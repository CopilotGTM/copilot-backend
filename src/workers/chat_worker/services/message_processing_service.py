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

import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlmodel import col

from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
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
from src.infra.messaging.redis_client import get_redis_client
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.base.worker_utils import get_organisation_name

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
    history: list[dict] = field(default_factory=list)


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
            logger.exception(
                "Message processing failed org=%s job=%s error=%s",
                self.organisation_id,
                job_id,
                e,
            )
            self._mark_job_failed(job_id, str(e))
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

            # Mark job as in progress
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

            # Load conversation history
            history_records = (
                session.query(ChatMessage)
                .filter(col(ChatMessage.session_id) == session_id)
                .order_by(col(ChatMessage.created_at).asc())
                .limit(30)
                .all()
            )

            # Convert history to serializable format (detach from session)
            history = []
            for msg in history_records:
                history.append(
                    {
                        "message_id": str(msg.message_id),
                        "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                        "content": msg.content,
                        "created_at": msg.created_at.isoformat() if msg.created_at else None,
                    }
                )

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
                user_message_session_id=user_message.session_id,
                user_message_parent_id=user_message.message_id,
                user_message_type=user_message.message_type,
                history=history,
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

            return AgentResult(
                answer=result_summary.get("answer"),
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
        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id) or org_name

        # Build context WITHOUT a session
        context = AgentContext(
            organisation_id=self.organisation_id,
            org_name=org_name,
            user_id=data.user_id,
            user_email=data.user_email,
            message_id=str(data.assistant_message_id),
        )

        async for frame in run_agent_for_chat(
            chat_type=data.chat_type,
            messages=data.history,
            context=context,
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
            # Create assistant message
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

        # Schedule stream cleanup
        self._schedule_stream_cleanup(data.job_id)

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

    def _schedule_stream_cleanup(self, job_id: UUID, delay_seconds: int = 30) -> None:
        """Schedule Redis stream cleanup in background thread."""
        cleanup_thread = threading.Thread(
            target=self._cleanup_job_stream,
            args=(job_id, delay_seconds),
            daemon=True,
        )
        cleanup_thread.start()

    def _cleanup_job_stream(self, job_id: UUID, delay_seconds: int) -> None:
        """Clean up Redis stream for completed job after a delay."""
        try:
            time.sleep(delay_seconds)
            stream_key = channel_for_chat_job(self.organisation_id, job_id)
            client = get_redis_client()
            result = client.delete(stream_key)

            if result:
                logger.info("Cleaned up stream for completed job: %s", stream_key)
            else:
                logger.debug("Stream was already cleaned up: %s", stream_key)

        except Exception as e:
            logger.warning("Failed to cleanup stream for job %s: %s", job_id, e)

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
