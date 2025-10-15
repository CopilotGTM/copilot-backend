from typing import Optional, List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Body, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.dependencies.rate_limit import rate_limit_user_write, rate_limit_user_read
from src.api.schemas.chat_models import (
    SendMessageRequest,
    SendMessageResponse,
    ChatMessageListItem,
    ChatMessagesPage,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.chat.chat_message_service import ChatMessageService
from src.workers.chat_worker.celery_app import app as chat_app
from src.service.chat.chat_message_sse_service import ChatMessageSSEService
from sse_starlette.sse import EventSourceResponse
from fastapi import Query
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessageRole,
    ChatMessageType,
)


logger = get_logger("chat_messages")
router = APIRouter()


async def get_chat_message_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    service = ChatMessageService(
        org_session, user_ids.organisation_id, user_ids.user_id
    )
    try:
        yield service
    finally:
        # Ensure proper cleanup of service resources
        if hasattr(service, "close") and callable(getattr(service, "close")):
            await service.close()


@router.post(
    "/send_message",
    response_model=GenericResponse[SendMessageResponse],
    tags=["chat_messages"],
    summary="Send a user message (persist + enqueue job)",
)
async def send_message(
    payload: SendMessageRequest = Body(...),
    service: ChatMessageService = Depends(get_chat_message_service),
    user_ids: UserIDs = Depends(get_current_user),
    _rl: None = Depends(rate_limit_user_write),
):
    try:
        # Map attachments to IDs, respecting order if provided
        attach_ids: Optional[List[UUID]] = payload.attachments

        # Create user message (this also creates and saves the job)
        user_message, job, assistant_message_id = await service.create_user_message(
            session_id=payload.session_id,
            content=payload.content,
            attachment_ids=attach_ids,
            message_type=payload.message_type,
            role=payload.role,
        )

        # Build response with all three IDs
        resp = SendMessageResponse(
            job_id=job.job_id,
            user_message_id=user_message.message_id,
            assistant_message_id=assistant_message_id,
        )

        # Enqueue background processing task
        logger.info(
            "Scheduling chat processing job org=%s job=%s",
            user_ids.organisation_id,
            job.job_id,
        )
        try:
            chat_app.send_task(
                "chat.process_message",
                args=[user_ids.organisation_id, job.job_id],
                queue="chat_messages",
                routing_key="chat_messages",
            )
            logger.info(
                "Enqueued chat processing job org=%s job=%s",
                user_ids.organisation_id,
                job.job_id,
            )
        except Exception as e:
            logger.error(
                "Failed to enqueue chat processing job %s: %s",
                job.job_id,
                e,
                exc_info=True,
            )
            await service.db.rollback()
            return Response.internal_server_error_response(
                "Failed to schedule message processing."
            )

        await service.db.commit()
        return Response.success_response("Message accepted", resp)
    except ValueError as e:
        await service.db.rollback()
        return Response.bad_request_error_response(str(e))
    except Exception as e:
        await service.db.rollback()
        logger.error("send_message failed: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to send message")


@router.get(
    "/message_events",
    tags=["chat_messages"],
    summary="SSE stream for a message job with consume-once delivery",
)
async def message_events(
    job_id: UUID,
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
):
    service = ChatMessageSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    logger.info(
        f"Client connected to chat SSE for org={user_ids.organisation_id} job={job_id} user={user_ids.user_id}"
    )

    # Extract Last-Event-ID header for replay (if provided by browser EventSource on reconnect)
    last_event_id = request.headers.get("last-event-id") or request.headers.get(
        "Last-Event-ID"
    )

    # If client provides Last-Event-ID, they want to resume from that point
    # In consume-once mode, this helps handle reconnections gracefully
    resume_from_id = last_event_id if last_event_id else None

    async def event_generator():
        try:
            # Use consume_once=True by default for one-time message delivery
            # Pass resume_from_id for potential reconnection handling
            async for ev in service.stream_events(
                consume_once=True, resume_from_id=resume_from_id
            ):
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
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(
        None, description="Opaque cursor returned by previous page (omit for first)"
    ),
    role: Optional[ChatMessageRole] = Query(None, description="Filter by role"),
    message_type: Optional[ChatMessageType] = Query(None, description="Filter by type"),
    service: ChatMessageService = Depends(get_chat_message_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    try:
        msgs, next_cursor = await service.list_messages(
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            role=role,
            message_type=message_type,
        )

        # Enrich with attachments + thumbnail URLs
        atts_by_msg = await service.get_message_attachments(msgs)
        atts_urls = await service.build_attachment_urls(attachments_by_msg=atts_by_msg)

        # Build enriched items using service method
        item_dicts = service.build_message_list_items(msgs, atts_urls)
        items: List[ChatMessageListItem] = [
            ChatMessageListItem(**item) for item in item_dicts
        ]
        page = ChatMessagesPage(items=items, cursor=next_cursor)
        return Response.success_response("Messages listed", page)
    except ValueError as e:
        return Response.bad_request_error_response(str(e))
    except Exception as e:
        logger.error("Unexpected error listing messages: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to list messages")
