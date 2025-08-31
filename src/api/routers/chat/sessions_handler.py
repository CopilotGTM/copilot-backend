from typing import Any, Dict

from fastapi import APIRouter, Depends, Body
from sqlmodel import Session

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.chat_models import (
    ChatSessionCreate,
    ChatSessionOut,
    DeleteChat,
    RenameChat,
    ChatSessionsPage,
)
from fastapi import Query
from typing import Optional
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.chat.chat_session_service import ChatSessionService


logger = get_logger("chat_sessions")
router = APIRouter()


async def get_chat_session_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: Session = Depends(get_org_session),
):
    service = ChatSessionService(org_session)
    # No async resources inside for now, but keep symmetry with other services
    try:
        yield service
    finally:
        pass


@router.post(
    "/create_chat",
    response_model=GenericResponse[ChatSessionOut],
    tags=["chats"],
    summary="Create a new chat session",
)
async def create_chat(
    payload: ChatSessionCreate = Body(...),
    service: ChatSessionService = Depends(get_chat_session_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        chat = service.create_session(
            user_id=user_ids.user_id,
            organisation_id=user_ids.organisation_id,
            title=payload.title,
            tags=payload.tags,
        )
        return Response.success_response("Chat created", data=ChatSessionOut.model_validate(chat))
    except ValueError as e:
        return Response.bad_request_error_response(str(e))
    except Exception as e:
        logger.error("Create chat failed: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to create chat session")


@router.delete(
    "/delete_chat",
    response_model=GenericResponse[ChatSessionOut],
    tags=["chats"],
    summary="Delete (soft) a chat session",
)
async def delete_chat(
    payload: DeleteChat = Body(...),
    service: ChatSessionService = Depends(get_chat_session_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        chat = service.delete_session(
            session_id=payload.session_id,
            user_id=user_ids.user_id,
            organisation_id=user_ids.organisation_id,
        )
        return Response.success_response("Chat deleted", data=ChatSessionOut.model_validate(chat))
    except ValueError as e:
        return Response.not_found_error_response(str(e))
    except Exception as e:
        logger.error("Delete chat failed: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to delete chat session")


@router.patch(
    "/rename_chat",
    response_model=GenericResponse[ChatSessionOut],
    tags=["chats"],
    summary="Rename a chat session",
)
async def rename_chat(
    payload: RenameChat = Body(...),
    service: ChatSessionService = Depends(get_chat_session_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        chat = service.rename_session(
            session_id=payload.session_id,
            user_id=user_ids.user_id,
            organisation_id=user_ids.organisation_id,
            title=payload.title,
        )
        return Response.success_response("Chat renamed", data=ChatSessionOut.model_validate(chat))
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            return Response.not_found_error_response(msg)
        return Response.bad_request_error_response(msg)
    except Exception as e:
        logger.error("Rename chat failed: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to rename chat session")


@router.get(
    "/list_chats",
    response_model=GenericResponse[ChatSessionsPage],
    tags=["chats"],
    summary="List chat sessions (paginated)",
)
async def list_chats(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None, description="Opaque cursor from previous page"),
    service: ChatSessionService = Depends(get_chat_session_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        items, next_cursor = service.list_sessions(
            user_id=user_ids.user_id,
            organisation_id=user_ids.organisation_id,
            limit=limit,
            cursor=cursor,
        )
        page = ChatSessionsPage(items=[ChatSessionOut.model_validate(i) for i in items], cursor=next_cursor)
        return Response.success_response("Chats listed", page)
    except Exception as e:
        logger.error("List chats failed: %s", e, exc_info=True)
        return Response.internal_server_error_response("Unable to list chat sessions")
