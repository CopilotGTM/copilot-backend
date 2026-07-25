"""Authenticated web-chat message, feedback, history, and SSE endpoints.

The send path commits the user message and recovery metadata before publishing
to Celery so a broker timeout cannot silently discard an accepted request.
"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.routers.chat.sessions_handler import _to_out
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.chat_models import (
    ChatMessageListItem,
    ChatMessagesPage,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessageRole,
    ChatMessageType,
)
from src.infra.utils.logger import get_logger
from src.service.chat.chat_message_service import ChatMessageService
from src.service.chat.chat_message_sse_service import ChatMessageSSEService
from src.workers.chat_worker.client import send_chat_task_async
from sse_starlette.sse import EventSourceResponse

logger = get_logger("chat_messages")
router = APIRouter()

_CHAT_QUEUE_PUBLISH_TIMEOUT_SECONDS = 4


async def get_chat_message_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Yield an org- and user-scoped chat service for one HTTP request."""
    service = ChatMessageService(org_session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        # Dependency cleanup owns the request-scoped database session even when
        # route handling raises before producing a response.
        if hasattr(service, "close") and callable(service.close):
            await service.close()


@router.post(
    "/send_message",
    response_model=GenericResponse[SendMessageResponse],
    tags=["chat_messages"],
    summary="Send a user message (persist + enqueue job)",
)
async def send_message(
    response: FastAPIResponse,
    payload: SendMessageRequest = Body(...),
    service: ChatMessageService = Depends(get_chat_message_service),
    user_ids: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[SendMessageResponse]:
    """Persist a user message and make its processing job recoverable.

    A successful response means the database owns enough state to republish the
    job; it does not promise that the first broker publication succeeded.
    """
    try:
        # Map attachments to IDs, respecting order if provided
        attach_ids: list[UUID] | None = payload.attachments

        message_type: ChatMessageType
        if payload.message_type is None:
            message_type = ChatMessageType.GENERAL_MESSAGE
        else:
            message_type = payload.message_type

        # The message and pending job share a transaction so recovery never
        # observes a job whose input message is missing.
        user_message, job, assistant_message_id = await service.create_user_message(
            session_id=payload.session_id,
            content=payload.content,
            user_timezone=user_ids.user_timezone,
            attachment_ids=attach_ids,
            message_type=message_type,
            role=payload.role,
        )

        # Build response with all three IDs
        resp = SendMessageResponse(
            job_id=job.job_id,
            user_message_id=user_message.message_id,
            assistant_message_id=UUID(assistant_message_id),
        )

        # Commit before enqueueing so the worker can always see the job/message.
        await service.db.commit()

        # Persist a lease before broker I/O. If the API dies after this commit,
        # the scheduled sweep can safely republish the still-pending job.
        metadata = dict(job.job_metadata or {})
        attempt = int(metadata.get("enqueue_attempts") or 0) + 1
        metadata.update(
            {
                "enqueue_status": "enqueuing",
                "enqueue_attempts": attempt,
                "celery_task_id": None,
                "last_enqueue_error": None,
            }
        )
        job.job_metadata = metadata
        service.db.add(job)
        await service.db.commit()

        logger.info(
            "Scheduling chat processing job org=%s job=%s",
            user_ids.organisation_id,
            job.job_id,
        )
        task_id: str | None = None
        enqueue_error: str | None = None
        try:
            task_id = await asyncio.wait_for(
                send_chat_task_async(
                    "chat.process_message",
                    args=[str(user_ids.organisation_id), str(job.job_id)],
                    queue="chat_messages",
                    routing_key="chat_messages",
                ),
                timeout=_CHAT_QUEUE_PUBLISH_TIMEOUT_SECONDS,
            )
            logger.info(
                "Enqueued chat processing job org=%s job=%s task_id=%s",
                user_ids.organisation_id,
                job.job_id,
                task_id,
            )
        except Exception as exc:
            enqueue_error = str(exc)[:1000]
            logger.error(
                "Failed to enqueue chat processing job %s: %s",
                job.job_id,
                exc,
                exc_info=True,
            )

        # Broker publication and PostgreSQL cannot commit atomically. Recording
        # either outcome lets recovery retry an unclaimed PENDING job.
        try:
            metadata = dict(job.job_metadata or {})
            if int(metadata.get("enqueue_attempts") or 0) == attempt:
                metadata.update(
                    {
                        "enqueue_status": "enqueued" if task_id else "failed",
                        "celery_task_id": task_id,
                        "last_enqueue_error": enqueue_error,
                    }
                )
                job.job_metadata = metadata
                service.db.add(job)
                await service.db.commit()
        except Exception:
            await service.db.rollback()
            logger.exception("Failed to record enqueue result for web chat job=%s", job.job_id)

        return Response.success_response("Message accepted", resp)
    except ValueError as e:
        await service.db.rollback()
        return Response.bad_request_error_response(str(e), response=response)
    except Exception as e:
        await service.db.rollback()
        logger.error("send_message failed: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to send message", response=response)


@router.get(
    "/message_events",
    tags=["chat_messages"],
    summary="Replayable SSE stream for a message job",
)
async def message_events(
    job_id: UUID,
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
):
    """Stream one user's job events with Last-Event-ID replay semantics."""
    service = ChatMessageSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    if not await service.can_stream():
        await service.close()
        raise HTTPException(status_code=404, detail="Chat job not found")
    logger.info(
        f"Client connected to chat SSE for org={user_ids.organisation_id} job={job_id} user={user_ids.user_id}"
    )

    # Extract Last-Event-ID header for replay (if provided by browser EventSource on reconnect)
    last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")

    resume_from_id = last_event_id if last_event_id else None

    async def event_generator():
        """Forward stored events and release the Redis stream client on exit."""
        try:
            async for ev in service.stream_events(resume_from_id=resume_from_id):
                yield ev
        finally:
            try:
                await service.close()
            except Exception:
                logger.debug("Chat SSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())


@router.get(
    "/messages/list",
    response_model=GenericResponse[ChatMessagesPage],
    tags=["chat_messages"],
    summary="List messages in a session (cursor-based pagination)",
)
async def list_messages(
    session_id: UUID,
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(
        None, description="Opaque cursor returned by previous page (omit for first)"
    ),
    role: ChatMessageRole | None = Query(None, description="Filter by role"),
    message_type: ChatMessageType | None = Query(None, description="Filter by type"),
    service: ChatMessageService = Depends(get_chat_message_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ChatMessagesPage]:
    """List readable messages and current session capabilities using an opaque cursor."""
    try:
        session, msgs, next_cursor = await service.list_messages(
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            role=role,
            message_type=message_type,
        )

        # Enrich with attachments + thumbnail URLs
        atts_by_msg = await service.get_message_attachments(session_id=session_id, messages=msgs)
        atts_urls = await service.build_attachment_urls(attachments_by_msg=atts_by_msg)

        # Build enriched items using service method
        item_dicts = service.build_message_list_items(msgs, atts_urls)
        items: list[ChatMessageListItem] = [ChatMessageListItem(**item) for item in item_dicts]
        page = ChatMessagesPage(
            session=_to_out(
                session,
                access="owner" if str(session.user_id) == str(service.user_id) else "viewer",
            ),
            items=items,
            cursor=next_cursor,
        )
        return Response.success_response("Messages listed", page)
    except ValueError as e:
        return Response.bad_request_error_response(str(e), response=response)
    except Exception as e:
        logger.error("Unexpected error listing messages: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to list messages", response=response)


@router.post(
    "/messages/feedback",
    response_model=GenericResponse[MessageFeedbackResponse],
    tags=["chat_messages"],
    summary="Provide feedback (thumbs up/down) for a message",
)
async def provide_message_feedback(
    response: FastAPIResponse,
    payload: MessageFeedbackRequest = Body(...),
    service: ChatMessageService = Depends(get_chat_message_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[MessageFeedbackResponse]:
    """Provide feedback on a chat message.

    Users can give thumbs up (+1), thumbs down (-1), or clear feedback (0).
    Only messages from sessions owned by the authenticated user can be updated.
    """
    try:
        # The service scopes the write through the owning session, preventing a
        # caller from rating another user's message by guessing its UUID.
        updated_message = await service.update_message_feedback(
            message_id=payload.message_id,
            feedback_score=payload.feedback_score,
        )

        # Commit the transaction
        await service.db.commit()

        # Build response data
        feedback_data = MessageFeedbackResponse(
            message_id=updated_message.message_id,
            feedback_score=updated_message.feedback_score,
        )

        logger.info(
            "Feedback updated for message=%s, score=%s",
            payload.message_id,
            payload.feedback_score,
        )

        return Response.success_response("Feedback updated successfully", feedback_data)

    except ValueError as e:
        await service.db.rollback()
        logger.warning("Invalid feedback request: %s", e)
        return Response.bad_request_error_response(str(e), response=response)
    except Exception as e:
        await service.db.rollback()
        logger.error("Failed to update message feedback: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to update feedback", response=response
        )
