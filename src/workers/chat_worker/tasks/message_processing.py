from __future__ import annotations

import asyncio
import contextlib
import time
from uuid import UUID

from celery.exceptions import Retry

from src.agents.orchestrator import run_agent_for_chat
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
    ChatMessageRole,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.infra.messaging.redis_channels import channel_for_chat_job
from src.infra.messaging.redis_client import get_redis_client
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.utils.logger import get_logger
from src.workers.chat_worker.celery_app import app

logger = get_logger("chat_message_processing_task")

# Frame type constants
FRAME_TYPE_ANSWER_DELTA = "answer.delta"
FRAME_TYPE_ANSWER_REPLACE = "answer.replace"
FRAME_TYPE_ANSWER_METRICS = "answer.metrics"
FRAME_TYPE_SUGGESTIONS_REPLACE = "suggestions.replace"
FRAME_TYPE_SOURCES_REPLACE = "sources.replace"
FRAME_TYPE_AGENT_SWITCH = "agent.switch"
FRAME_TYPE_QUESTION_PROMPT = "question.prompt"
FRAME_TYPE_PROGRESS_UPDATE = "progress.update"
FRAME_TYPE_TOOL_USAGE = "tool.usage"
FRAME_TYPE_THINKING_UPDATE = "thinking.update"
FRAME_TYPE_FINALIZE = "finalize"
FRAME_TYPE_VISUALIZATION = "visualization"
FRAME_TYPE_STRUCTURED_DATA = "structured_data"
FRAME_TYPE_TASK_LIST = "task_list"


def _cleanup_job_stream(organisation_id: UUID, job_id: UUID, delay_seconds: int = 30) -> None:
    """
    Clean up Redis stream for completed job after a delay.
    This prevents immediate cleanup in case of reconnections.
    """
    try:
        import time

        time.sleep(delay_seconds)  # Simple delay - in production, consider using Celery's countdown

        stream_key = channel_for_chat_job(organisation_id, job_id)
        client = get_redis_client()
        result = client.delete(stream_key)

        if result:
            logger.info("Cleaned up stream for completed job: %s", stream_key)
        else:
            logger.debug("Stream was already cleaned up: %s", stream_key)

    except Exception as e:
        logger.warning("Failed to cleanup stream for job %s: %s", job_id, e)


def _get_sync_session(organisation_id: UUID):
    """Acquire a sync session for the given organisation.

    Returns (session_gen, session), both must be closed by caller.
    """
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    return session_gen, session


def _load_job(session, job_id: UUID) -> ChatMessageJob:
    job = session.get(ChatMessageJob, job_id)
    if not job:
        raise RuntimeError(f"Job {job_id} not found")
    return job


class RedisPublisher:
    """Publish JSON frames to a Redis Stream for a specific job."""

    def __init__(self, organisation_id: UUID, job_id: UUID, maxlen: int = 500):
        self._stream_key = channel_for_chat_job(organisation_id, job_id)
        self._maxlen = maxlen

    def publish(self, frame: dict) -> None:
        if xadd_json_frame(self._stream_key, frame, maxlen=self._maxlen, approximate=True) is None:
            # Include job id if present in frame for easier debugging
            logger.warning("Failed to XADD frame for job %s", frame.get("job_id"))


def _process_frame_data(data: dict, result_summary: dict) -> None:
    """Process individual frame data and update result summary."""
    ftype = data.get("type")

    if ftype in [FRAME_TYPE_ANSWER_DELTA, FRAME_TYPE_ANSWER_REPLACE]:
        _process_answer_frame(data, result_summary)
    elif ftype == FRAME_TYPE_ANSWER_METRICS:
        _process_metrics_frame(data, result_summary)
    elif ftype == FRAME_TYPE_SUGGESTIONS_REPLACE:
        _process_suggestions_frame(data, result_summary)
    elif ftype == FRAME_TYPE_SOURCES_REPLACE:
        _process_sources_frame(data, result_summary)
    elif ftype == FRAME_TYPE_AGENT_SWITCH:
        _process_agent_switch_frame(data, result_summary)
    elif ftype == FRAME_TYPE_QUESTION_PROMPT:
        _process_question_frame(data, result_summary)
    elif ftype == FRAME_TYPE_PROGRESS_UPDATE:
        _process_progress_frame(data, result_summary)
    elif ftype == FRAME_TYPE_TOOL_USAGE:
        _process_tool_usage_frame(data, result_summary)
    elif ftype == FRAME_TYPE_THINKING_UPDATE:
        _process_thinking_frame(data, result_summary)
    elif ftype == FRAME_TYPE_VISUALIZATION:
        _process_visualization_frame(data, result_summary)
    elif ftype == FRAME_TYPE_STRUCTURED_DATA:
        _process_structured_data_frame(data, result_summary)
    elif ftype == FRAME_TYPE_TASK_LIST:
        _process_task_list_frame(data, result_summary)


def _process_answer_frame(data: dict, result_summary: dict) -> None:
    """Process answer delta or replace frames."""
    if data.get("type") == FRAME_TYPE_ANSWER_DELTA:
        chunk = data.get("content") or ""
        result_summary["answer_parts"].append(chunk)
    elif data.get("type") == FRAME_TYPE_ANSWER_REPLACE:
        result_summary["final_answer"] = data.get("content") or ""


def _process_metrics_frame(data: dict, result_summary: dict) -> None:
    """Process answer metrics frames."""
    result_summary["confidence"] = data.get("confidence")
    if not result_summary["sources"] and data.get("citations"):
        result_summary["sources"] = data.get("citations")


def _process_suggestions_frame(data: dict, result_summary: dict) -> None:
    """Process suggestions frames."""
    items = data.get("items") or []
    suggestions: list[str] = []
    for i in items:
        if isinstance(i, str):
            suggestions.append(i)
        elif isinstance(i, dict):
            # Support both {label: str} objects and other common keys
            label = i.get("label") or i.get("text") or i.get("title")
            if label:
                suggestions.append(str(label))
    result_summary["suggestions"] = suggestions or None


def _process_sources_frame(data: dict, result_summary: dict) -> None:
    """Process sources frames."""
    result_summary["sources"] = data.get("sources")


def _process_agent_switch_frame(data: dict, result_summary: dict) -> None:
    """Process agent switch frames."""
    result_summary["agent_switch_request"] = {
        "to_agent": data.get("to_agent"),
        "from_agent": data.get("from_agent"),
        "reason": data.get("reason"),
        "confidence": data.get("confidence"),
    }


def _process_question_frame(data: dict, result_summary: dict) -> None:
    """Process question prompt frames."""
    result_summary["user_questions"].append(
        {
            "prompt": data.get("prompt"),
            "fields": data.get("fields"),
            "await_user_response": data.get("await_user_response", True),
        }
    )


def _process_progress_frame(data: dict, result_summary: dict) -> None:
    """Process progress update frames."""
    result_summary["progress_updates"].append(
        {
            "stage": data.get("stage"),
            "message": data.get("message"),
            "progress": data.get("progress"),
            "eta_seconds": data.get("eta_seconds"),
        }
    )


def _process_tool_usage_frame(data: dict, result_summary: dict) -> None:
    """Process tool usage frames."""
    result_summary["tool_usage"].append(
        {
            "tool_name": data.get("tool_name"),
            "status": data.get("status"),
            "duration_ms": data.get("duration_ms"),
            "result_summary": data.get("result_summary"),
        }
    )


def _process_thinking_frame(data: dict, result_summary: dict) -> None:
    """Process thinking update frames."""
    result_summary["thinking_updates"].append(
        {"thought": data.get("thought"), "stage": data.get("stage")}
    )


def _process_visualization_frame(data: dict, result_summary: dict) -> None:
    """Process visualization frames (Mermaid diagrams, charts, etc.)."""
    result_summary["visualizations"].append(
        {
            "visualization_type": data.get("visualization_type"),
            "diagram_type": data.get("diagram_type"),
            "content": data.get("content"),
            "title": data.get("title"),
            "description": data.get("description"),
        }
    )


def _process_structured_data_frame(data: dict, result_summary: dict) -> None:
    """Process structured data frames (tables, comparisons, lists)."""
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


def _process_task_list_frame(data: dict, result_summary: dict) -> None:
    """Process task list frames for agent work tracking.

    Each task_list frame replaces the previous one (latest status).
    """
    tasks = data.get("tasks") or []
    result_summary["task_list"] = [
        {
            "id": task.get("id"),
            "task": task.get("task"),
            "status": task.get("status"),
        }
        for task in tasks
        if isinstance(task, dict)
    ]


async def _consume_agent_stream(
    chat_type,
    history,
    publisher: RedisPublisher,
    job: ChatMessageJob,
    organisation_id: UUID,
    user_id: UUID | None = None,
) -> dict:
    """Run the async agent (async generator), stream frames, and return a summary.

    Returns a dict with keys: answer, confidence, suggestions, sources, agent_switch_request, user_questions.
    """
    result_summary = {
        "answer_parts": [],
        "final_answer": None,
        "confidence": None,
        "suggestions": None,
        "sources": None,
        "agent_switch_request": None,
        "user_questions": [],
        "progress_updates": [],
        "tool_usage": [],
        "thinking_updates": [],
        "visualizations": [],
        "structured_data": [],
        "task_list": [],
    }

    # Extract assistant_message_id from job metadata for SSE frames
    assistant_message_id = job.job_metadata.get("assistant_message_id")

    sequence_counter = 0

    # Acquire async session for agent tools
    from src.agents.context import AgentContext
    from src.api.dependencies.organisation_dependencies import acquire_org_async_session

    async for session in acquire_org_async_session(organisation_id):
        # Build context once for the entire agent execution
        # TODO: Fetch org_name from organisation database in future
        context = AgentContext(
            session=session,
            organisation_id=organisation_id,
            org_name="CopilotGTM",
            user_id=user_id,
            message_id=str(assistant_message_id),
        )

        async for frame in run_agent_for_chat(
            chat_type=chat_type,
            messages=history,  # type: ignore[arg-type]
            context=context,
        ):
            try:
                data = frame.model_dump()
            except Exception:
                # Create a closure to capture current frame value
                def get_dict(current_frame=frame):
                    return getattr(current_frame, "dict", lambda: dict(current_frame))()

                data = get_dict()

            # Transform to production-level frame structure with job_id and assistant_message_id
            production_frame = {
                **data,  # Copy all frame data
                "job_id": str(job.job_id),
                "assistant_message_id": str(assistant_message_id),
                "version": "1.0",
                "timestamp": time.time(),
                "sequence": sequence_counter,
            }

            sequence_counter += 1

            # Publish frame to Redis Stream
            publisher.publish(production_frame)

            # Process frame data for summary (using original data structure)
            _process_frame_data(data, result_summary)

        return {
            "answer": (
                "".join(result_summary["answer_parts"])
                if result_summary["answer_parts"]
                else result_summary["final_answer"]
            ),
            "confidence": result_summary["confidence"],
            "suggestions": result_summary["suggestions"],
            "sources": result_summary["sources"],
            "agent_switch_request": result_summary["agent_switch_request"],
            "user_questions": result_summary["user_questions"],
            "progress_updates": result_summary["progress_updates"],
            "tool_usage": result_summary["tool_usage"],
            "thinking_updates": result_summary["thinking_updates"],
            "visualizations": result_summary["visualizations"],
            "structured_data": result_summary["structured_data"],
            "task_list": result_summary["task_list"],
        }


@app.task(name="chat.process_message", acks_late=True, bind=True, max_retries=5)
def process_message(self, organisation_id: UUID, job_id: UUID):
    """Process a chat message job."""
    logger.info("Processing chat message job org=%s job=%s", organisation_id, job_id)

    session_gen, session = _get_sync_session(organisation_id)
    publisher = RedisPublisher(organisation_id, job_id)

    try:
        # Load and validate job. This can race if the API enqueued before DB commit.
        try:
            job = _load_job(session, job_id)
        except RuntimeError as e:
            if " not found" in str(e):
                countdown = min(2**self.request.retries, 30)
                logger.warning(
                    "Chat job %s not found yet (org=%s). Retrying in %ss (attempt %s/%s).",
                    job_id,
                    organisation_id,
                    countdown,
                    self.request.retries + 1,
                    self.max_retries,
                )
                raise self.retry(exc=e, countdown=countdown) from e
            raise
        _update_job_status(session, job, JobStatus.IN_PROGRESS)

        # Load session and conversation data (assistant_message_id from metadata, not created yet)
        _, chat_session, history, user_message, assistant_message_id = _load_conversation_data(
            session, job
        )

        # Execute agent and get results (pass organisation_id and user_id for tool access)
        result_summary = _execute_agent_workflow(
            chat_session, history, publisher, job, organisation_id, chat_session.user_id
        )

        # Publish final frames and create assistant message with results
        _publish_final_frames(publisher, job, result_summary)
        assistant_message = _create_assistant_message(
            session, user_message, assistant_message_id, result_summary
        )
        _finalize_job(session, job, result_summary, organisation_id, assistant_message)

        logger.info("Job %s completed", job_id)

    except Retry:
        raise
    except Exception as e:
        _handle_job_failure(session, job_id, e)
        raise
    finally:
        _cleanup_resources(session, session_gen)


def _update_job_status(session, job: ChatMessageJob, status: JobStatus) -> None:
    """Update job status and commit."""
    job.status = status
    session.add(job)
    session.commit()


def _load_conversation_data(session, job: ChatMessageJob):
    """Load session, chat session, history, and user message. Returns assistant_message_id for creation."""
    # Extract all IDs from job metadata (set by API layer)
    md = job.job_metadata or {}
    session_id = md.get("session_id")
    user_message_id = md.get("user_message_id")
    assistant_message_id = md.get("assistant_message_id")

    if not session_id:
        raise RuntimeError("Missing session_id in job_metadata")
    if not user_message_id:
        raise RuntimeError("Missing user_message_id in job_metadata")
    if not assistant_message_id:
        raise RuntimeError("Missing assistant_message_id in job_metadata")

    try:
        session_id = UUID(str(session_id))
        user_message_id = UUID(str(user_message_id))
        assistant_message_id = UUID(str(assistant_message_id))
    except Exception as e:
        raise RuntimeError("Invalid UUID in job_metadata") from e

    # Load chat session for chat_type
    chat_session: ChatSession | None = session.get(ChatSession, session_id)
    if not chat_session:
        raise RuntimeError(f"Session {session_id} not found")

    # Load user message
    user_message: ChatMessage | None = session.get(ChatMessage, user_message_id)
    if not user_message:
        raise RuntimeError(f"User message {user_message_id} not found for job {job.job_id}")

    # Load recent conversation history (ordered asc, max 30)
    history = (
        session.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(30)
        .all()
    )

    logger.info(
        "Loaded conversation data for job %s: session=%s user_msg=%s asst_msg_id=%s history_count=%d",
        job.job_id,
        session_id,
        user_message_id,
        assistant_message_id,
        len(history),
    )

    return session_id, chat_session, history, user_message, assistant_message_id


def _execute_agent_workflow(chat_session, history, publisher, job, organisation_id, user_id=None):
    """Execute agent workflow and handle errors."""
    try:
        result_summary = asyncio.run(
            _consume_agent_stream(
                chat_session.chat_type,
                history,
                publisher,
                job,
                organisation_id,
                user_id,
            )
        )

        # Ensure result_summary is never None
        if result_summary is None:
            logger.error(f"_consume_agent_stream returned None for job {job.job_id}")
            return _create_error_summary()

        return result_summary

    except Exception as e:
        logger.error(f"Error in _consume_agent_stream for job {job.job_id}: {e}")
        return _create_error_summary()


def _create_error_summary() -> dict:
    """Create error result summary."""
    return {
        "answer": "I apologize, but I encountered an error processing your request.",
        "confidence": None,
        "suggestions": None,
        "sources": None,
        "agent_switch_request": None,
        "user_questions": [],
        "progress_updates": [],
        "tool_usage": [],
        "thinking_updates": [],
        "visualizations": [],
        "structured_data": [],
        "task_list": [],
    }


def _publish_final_frames(publisher: RedisPublisher, job: ChatMessageJob, result_summary: dict):
    """Publish final frames with production metadata."""
    assistant_message_id = job.job_metadata.get("assistant_message_id")

    base_frame = {
        "job_id": str(job.job_id),
        "assistant_message_id": str(assistant_message_id),
        "version": "1.0",
        "timestamp": time.time(),
    }

    if result_summary.get("answer"):
        publisher.publish(
            {
                **base_frame,
                "type": FRAME_TYPE_ANSWER_REPLACE,
                "content": result_summary.get("answer") or "",
                "format": "markdown",
            }
        )

    if result_summary.get("confidence") is not None:
        publisher.publish(
            {
                **base_frame,
                "type": FRAME_TYPE_ANSWER_METRICS,
                "confidence": result_summary.get("confidence"),
            }
        )

    if result_summary.get("suggestions"):
        publisher.publish(
            {
                **base_frame,
                "type": FRAME_TYPE_SUGGESTIONS_REPLACE,
                "items": result_summary.get("suggestions") or [],
            }
        )


def _create_assistant_message(
    session, user_message: ChatMessage, assistant_message_id: UUID, result_summary: dict
) -> ChatMessage:
    """Create the assistant message with the pre-generated ID and agent results."""
    # Create assistant message with the ID provided by API layer
    assistant_message = ChatMessage(
        message_id=assistant_message_id,  # Use the pre-generated ID
        session_id=user_message.session_id,
        parent_message_id=user_message.message_id,
        role=ChatMessageRole.ASSISTANT,
        message_type=user_message.message_type,
        content={"text": (result_summary.get("answer") or "")},
        extra={
            "confidence_score": result_summary.get("confidence"),
            "has_user_questions": bool(result_summary.get("user_questions")),
            "agent_switch_requested": bool(result_summary.get("agent_switch_request")),
            "progress_tracked": bool(result_summary.get("progress_updates")),
            "tool_usage_count": len(result_summary.get("tool_usage") or []),
            "thinking_updates_count": len(result_summary.get("thinking_updates") or []),
            "visualizations_count": len(result_summary.get("visualizations") or []),
            "structured_data_count": len(result_summary.get("structured_data") or []),
            "task_list_count": len(result_summary.get("task_list") or []),
        },
        agent_data={
            "sources": result_summary.get("sources"),
            "suggested_questions": result_summary.get("suggestions") or [],
            "agent_switch_request": result_summary.get("agent_switch_request"),
            "user_questions": result_summary.get("user_questions") or [],
            "progress_updates": result_summary.get("progress_updates") or [],
            "tool_usage": result_summary.get("tool_usage") or [],
            "thinking_updates": result_summary.get("thinking_updates") or [],
            "visualizations": result_summary.get("visualizations") or [],
            "structured_data": result_summary.get("structured_data") or [],
            "task_list": result_summary.get("task_list") or [],
        },
    )

    session.add(assistant_message)
    session.flush()

    logger.info(
        "Created assistant message %s with answer (length=%d)",
        assistant_message.message_id,
        len(result_summary.get("answer") or ""),
    )

    return assistant_message


def _finalize_job(
    session,
    job: ChatMessageJob,
    result_summary: dict,
    organisation_id: UUID,
    assistant_message: ChatMessage,
):
    """Finalize job with enhanced result data."""
    # Use the assistant message passed from the workflow
    job.result_data = {
        "answer": result_summary.get("answer"),
        "confidence": result_summary.get("confidence"),
        "sources": result_summary.get("sources"),
        "suggested_questions": result_summary.get("suggestions"),
        "assistant_message_id": str(assistant_message.message_id),
        "agent_switch_request": result_summary.get("agent_switch_request"),
        "user_questions_count": len(result_summary.get("user_questions") or []),
        "progress_updates_count": len(result_summary.get("progress_updates") or []),
        "tool_usage_count": len(result_summary.get("tool_usage") or []),
        "thinking_updates_count": len(result_summary.get("thinking_updates") or []),
        "visualizations_count": len(result_summary.get("visualizations") or []),
        "structured_data_count": len(result_summary.get("structured_data") or []),
        "task_list": result_summary.get("task_list") or [],
    }
    job.status = JobStatus.COMPLETED
    session.add(job)
    session.commit()

    # Publish final frame with job_id
    publisher = RedisPublisher(organisation_id, job.job_id)
    publisher.publish(
        {
            "type": FRAME_TYPE_FINALIZE,
            "job_id": str(job.job_id),
            "assistant_message_id": str(assistant_message.message_id),
            "version": "1.0",
            "timestamp": time.time(),
            "reason": "done",
        }
    )

    # Schedule stream cleanup in a background thread to avoid blocking
    import threading

    cleanup_thread = threading.Thread(
        target=_cleanup_job_stream, args=(organisation_id, job.job_id, 30), daemon=True
    )
    cleanup_thread.start()
    logger.info(
        "Finalized job %s with assistant message %s",
        job.job_id,
        assistant_message.message_id,
    )


def _handle_job_failure(session, job_id: UUID, error: Exception):
    """Handle job failure by rolling back and marking job as failed."""
    with contextlib.suppress(Exception):
        session.rollback()

    logger.exception("Job %s failed: %s", job_id, error)

    try:
        job = session.get(ChatMessageJob, job_id)
        if job:
            job.status = JobStatus.FAILED
            job.error_message = str(error)[:500]
            session.add(job)
            session.commit()
    except Exception:
        pass


def _cleanup_resources(session, session_gen):
    """Clean up database resources."""
    with contextlib.suppress(Exception):
        session.close()
    with contextlib.suppress(Exception):
        session_gen.close()
