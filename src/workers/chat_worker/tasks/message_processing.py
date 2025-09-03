"""Chat message processing task.

Transitions the job through states, runs the appropriate chat agent to
generate a structured answer, persists results, and streams JSON frames
over Redis for SSE consumption by the frontend.
"""
from __future__ import annotations

from src.workers.chat_worker.celery_app import app
from src.infra.utils.logger import get_logger
import json
from typing import List

from src.infra.utils.database import get_org_session
from src.infra.messaging.redis_channels import channel_for_chat_job
from src.infra.messaging.redis_streams import xadd_json_frame
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.domain.models.organisation_tables.chat_message_models import ChatMessage, ChatMessageRole
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.agents.orchestrator import run_agent_for_chat

logger = get_logger("chat_message_processing_task")


@app.task(name="chat.process_message", bind=True, acks_late=True, max_retries=3)
def process_message(self, organisation_id: str, job_id: str):
    logger.info("Processing chat message job org=%s job=%s", organisation_id, job_id)
    session_gen = get_org_session(organisation_id)
    session = next(session_gen)
    try:
        job = session.get(ChatMessageJob, job_id)
        if not job:
            logger.warning("Job %s not found", job_id)
            return

        # Prepare Redis Stream key (reuse channel naming function)
        stream_key = channel_for_chat_job(organisation_id, job_id)

        def publish_frame(frame: dict):
            # Append frame to Redis Stream (durable backlog + replay)
            if xadd_json_frame(stream_key, frame, maxlen=500, approximate=True) is None:
                logger.warning("Failed to XADD frame for job %s", job_id)

        # Transition to in_progress
        job.status = JobStatus.IN_PROGRESS
        session.add(job)
        session.commit()
        publish_frame(
            {
                "type": "job.status",
                "message_id": str(job.message_id),
                "job_id": str(job.job_id),
                "status": JobStatus.IN_PROGRESS.value,
            }
        )

        # Load message + session context
        message: ChatMessage | None = session.get(ChatMessage, job.message_id)
        if not message:
            raise RuntimeError(f"Message {job.message_id} not found for job {job.job_id}")
        chat_session: ChatSession | None = session.get(ChatSession, message.session_id)
        if not chat_session:
            raise RuntimeError(f"Session {message.session_id} not found")

        # Build recent conversation (ordered by created_at)
        # Keep it light; last 30 messages
        from sqlmodel import select
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == message.session_id)
            .order_by(ChatMessage.created_at.asc())
        )
        history: List[ChatMessage] = session.exec(stmt).all()

        # Run agent (structured output)
        try:
            import asyncio

            result = asyncio.run(
                run_agent_for_chat(
                    chat_type=chat_session.chat_type,  # type: ignore[arg-type]
                    messages=history,
                )
            )
        except RuntimeError as re:
            # If already inside an event loop, use nested policy
            logger.debug("Asyncio runtime warning: %s", re)
            result = asyncio.get_event_loop().run_until_complete(
                run_agent_for_chat(
                    chat_type=chat_session.chat_type,  # type: ignore[arg-type]
                    messages=history,
                )
            )

        # Publish frames from structured result
        if result.answer:
            publish_frame(
                {
                    "type": "answer.replace",
                    "message_id": str(job.message_id),
                    "content": result.answer,
                    "format": "markdown",
                }
            )

        if result.confidence_score is not None:
            publish_frame(
                {
                    "type": "answer.metrics",
                    "message_id": str(job.message_id),
                    "confidence": result.confidence_score,
                }
            )

        if result.suggested_questions:
            publish_frame(
                {
                    "type": "suggestions.replace",
                    "message_id": str(job.message_id),
                    "items": [
                        {"label": q, "payload": {"intent": "suggested_question", "text": q}}
                        for q in result.suggested_questions
                    ],
                }
            )

        # Persist assistant message
        assistant_msg = ChatMessage(
            session_id=message.session_id,
            parent_message_id=message.message_id,
            role=ChatMessageRole.ASSISTANT,
            message_type=message.message_type,
            content={"text": result.answer or ""},
            extra={"confidence_score": result.confidence_score},
            agent_data={
                "sources": [s.model_dump() for s in (result.sources or [])],
                "suggested_questions": result.suggested_questions or [],
            },
        )
        session.add(assistant_msg)
        session.flush()

        # Finalize job
        job.result_data = {
            "answer": result.answer,
            "confidence": result.confidence_score,
            "sources": [s.model_dump() for s in (result.sources or [])] if result.sources else None,
            "suggested_questions": result.suggested_questions,
            "assistant_message_id": str(assistant_msg.message_id),
        }
        job.status = JobStatus.COMPLETED
        session.add(job)
        session.commit()

        publish_frame(
            {
                "type": "finalize",
                "message_id": str(job.message_id),
                "job_id": str(job.job_id),
                "reason": "done",
            }
        )

        logger.info("Job %s completed", job_id)
    except Exception as e:
        session.rollback()
        logger.exception("Job %s failed: %s", job_id, e)
        try:
            job = session.get(ChatMessageJob, job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error_message = str(e)[:500]
                session.add(job)
                session.commit()
        except Exception:
            pass
        raise
    finally:
        try:
            session.close()
        except Exception:
            pass