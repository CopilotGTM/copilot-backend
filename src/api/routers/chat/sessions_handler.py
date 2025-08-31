from typing import Any, Dict

from fastapi import APIRouter, Depends, Body
from sqlmodel import Session

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.chat_models import ChatSessionCreate, ChatSessionOut
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
