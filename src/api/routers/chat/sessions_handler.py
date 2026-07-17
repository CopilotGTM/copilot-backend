from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.chat_models import (
    ChatSessionCapabilities,
    ChatSessionCreate,
    ChatSessionOut,
    ChatSessionShareOut,
    ChatSessionsPage,
    ChatSharedByOut,
    ChatSharedWithMeOut,
    DeleteChat,
    FavoriteChat,
    RenameChat,
    SharedChatsPage,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.chat.chat_session_service import ChatSessionService

logger = get_logger("chat_sessions")
router = APIRouter()

# Share-management failures deliberately use one response so callers cannot
# distinguish an unknown viewer from another owner's existing chat.
_SHARE_NOT_FOUND_MESSAGE = "Chat session or viewer not found"


async def get_chat_session_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    service = ChatSessionService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        master_db=master_session,
    )
    try:
        yield service
    finally:
        # nothing to cleanup currently
        ...


def _to_out(chat, *, access: Literal["owner", "viewer"] = "owner") -> ChatSessionOut:
    is_owner = access == "owner"
    props = getattr(chat, "properties", {}) or {}
    is_fav = bool(props.get("favorite", False))
    return ChatSessionOut(
        session_id=chat.session_id,
        title=chat.title,
        chat_type=str(getattr(chat, "chat_type", "GENERAL_CHAT")),
        is_favorite=is_fav,
        tags=getattr(chat, "tags", []) or [],
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        access=access,
        capabilities=ChatSessionCapabilities(
            read=True,
            send=is_owner,
            rename=is_owner,
            delete=is_owner,
            manage_shares=is_owner,
        ),
    )


@router.post(
    "/create_chat",
    response_model=GenericResponse[ChatSessionOut],
    tags=["chats"],
    summary="Create a new chat session",
)
async def create_chat(
    response: FastAPIResponse,
    payload: ChatSessionCreate = Body(...),
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[ChatSessionOut]:
    try:
        chat = await service.create_session(
            title=payload.title,
            tags=payload.tags,
        )
        return Response.success_response("Chat created", data=_to_out(chat))
    except ValueError as e:
        return Response.bad_request_error_response(str(e), response=response)
    except Exception as e:
        logger.error("Create chat failed: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to create chat session", response=response
        )


@router.delete(
    "/delete_chat",
    response_model=GenericResponse[ChatSessionOut],
    tags=["chats"],
    summary="Delete (soft) a chat session",
)
async def delete_chat(
    response: FastAPIResponse,
    payload: DeleteChat = Body(...),
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[ChatSessionOut]:
    try:
        chat = await service.delete_session(
            session_id=payload.session_id,
        )
        return Response.success_response("Chat deleted", data=_to_out(chat))
    except ValueError as e:
        return Response.not_found_error_response(str(e), response=response)
    except Exception as e:
        logger.error("Delete chat failed: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to delete chat session", response=response
        )


@router.patch(
    "/rename_chat",
    response_model=GenericResponse[ChatSessionOut],
    tags=["chats"],
    summary="Rename a chat session",
)
async def rename_chat(
    response: FastAPIResponse,
    payload: RenameChat = Body(...),
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[ChatSessionOut]:
    try:
        chat = await service.rename_session(
            session_id=payload.session_id,
            title=payload.title,
        )
        return Response.success_response("Chat renamed", data=_to_out(chat))
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            return Response.not_found_error_response(msg, response=response)
        return Response.bad_request_error_response(msg, response=response)
    except Exception as e:
        logger.error("Rename chat failed: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to rename chat session", response=response
        )


@router.get(
    "/list_chats",
    response_model=GenericResponse[ChatSessionsPage],
    tags=["chats"],
    summary="List chat sessions (paginated)",
)
async def list_chats(
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, description="Opaque cursor from previous page"),
    chat_type: str | None = Query(None, description="Filter by chat_type (e.g. VOICE_CHAT)"),
    scope: Literal["all", "owned", "shared"] = Query(
        "all", description="Filter by access: all, owned, or shared"
    ),
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ChatSessionsPage]:
    try:
        items, next_cursor = await service.list_sessions(
            limit=limit,
            cursor=cursor,
            chat_type=chat_type,
            scope=scope,
        )
        page = ChatSessionsPage(
            items=[
                _to_out(
                    item,
                    access="owner" if str(item.user_id) == str(service.user_id) else "viewer",
                )
                for item in items
            ],
            cursor=next_cursor,
        )
        return Response.success_response("Chats listed", data=page)
    except Exception as e:
        logger.error("List chats failed: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to list chat sessions", response=response
        )


@router.get(
    "/shared",
    response_model=GenericResponse[SharedChatsPage],
    tags=["chats"],
    summary="List chats shared with the current user",
)
async def list_shared_chats(
    response: FastAPIResponse,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, description="Opaque cursor from previous page"),
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[SharedChatsPage]:
    """List shared chats with grantor metadata for the settings screen."""
    try:
        rows, next_cursor = await service.list_shared_with_me(limit=limit, cursor=cursor)
        items = []
        for chat, grant, shared_by in rows:
            session = _to_out(chat, access="viewer")
            items.append(
                ChatSharedWithMeOut(
                    **session.model_dump(),
                    shared_at=grant.created_at,
                    shared_by=ChatSharedByOut(
                        user_id=grant.granted_by_user_id,
                        name=(
                            f"{shared_by.first_name} {shared_by.last_name}".strip() or None
                            if shared_by
                            else None
                        ),
                        email=str(shared_by.email) if shared_by else None,
                    ),
                )
            )
        page = SharedChatsPage(items=items, cursor=next_cursor)
        return Response.success_response("Shared chats listed", data=page)
    except Exception as exc:
        logger.error("List shared chats failed: %s", exc, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to list shared chats", response=response
        )


@router.get(
    "/search_chats",
    response_model=GenericResponse[ChatSessionsPage],
    tags=["chats"],
    summary="Search chat sessions by title (typeahead)",
)
async def search_chats(
    response: FastAPIResponse,
    q: str = Query(..., min_length=1, description="Search text in title (case-insensitive)"),
    limit: int = Query(10, ge=1, le=50),
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ChatSessionsPage]:
    try:
        items = await service.search_sessions(
            q=q,
            limit=limit,
        )
        page = ChatSessionsPage(items=[_to_out(i) for i in items], cursor=None)
        return Response.success_response("Chats searched", data=page)
    except Exception as e:
        logger.error("Search chats failed: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to search chat sessions", response=response
        )


@router.patch(
    "/favorite_chat",
    response_model=GenericResponse[ChatSessionOut],
    tags=["chats"],
    summary="Mark or unmark a chat as favorite",
)
async def favorite_chat(
    response: FastAPIResponse,
    payload: FavoriteChat = Body(...),
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[ChatSessionOut]:
    try:
        chat = await service.set_favorite(
            session_id=payload.session_id,
            favorite=payload.favorite,
        )
        msg = "Chat favorited" if payload.favorite else "Chat unfavorited"
        return Response.success_response(msg, data=_to_out(chat))
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            return Response.not_found_error_response(msg, response=response)
        return Response.bad_request_error_response(msg, response=response)
    except Exception as e:
        logger.error("Favorite toggle failed: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to update favorite state", response=response
        )


@router.put(
    "/{session_id}/shares/{viewer_user_id}",
    response_model=GenericResponse[ChatSessionShareOut],
    tags=["chats"],
    summary="Grant read-only access to a chat session",
)
async def grant_chat_viewer(
    response: FastAPIResponse,
    session_id: UUID,
    viewer_user_id: UUID,
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[ChatSessionShareOut]:
    """Grant one same-organisation user read-only access to an owned chat."""
    try:
        grant = await service.grant_viewer(
            session_id=session_id,
            viewer_user_id=viewer_user_id,
        )
        data = ChatSessionShareOut.model_validate(grant)
        return Response.success_response("Chat viewer granted", data=data)
    except ValueError as exc:
        message = str(exc)
        if "owner already has access" in message.lower():
            return Response.bad_request_error_response(message, response=response)
        return Response.not_found_error_response(
            _SHARE_NOT_FOUND_MESSAGE,
            response=response,
        )
    except Exception as exc:
        logger.error("Grant chat viewer failed: %s", exc, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to grant chat viewer",
            response=response,
        )


@router.get(
    "/{session_id}/shares",
    response_model=GenericResponse[list[ChatSessionShareOut]],
    tags=["chats"],
    summary="List read-only viewers for a chat session",
)
async def list_chat_viewers(
    response: FastAPIResponse,
    session_id: UUID,
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[ChatSessionShareOut]]:
    """List explicit viewers for a chat owned by the authenticated caller."""
    try:
        grants = await service.list_viewers(session_id=session_id)
        data = [ChatSessionShareOut.model_validate(grant) for grant in grants]
        return Response.success_response("Chat viewers listed", data=data)
    except ValueError:
        return Response.not_found_error_response(
            _SHARE_NOT_FOUND_MESSAGE,
            response=response,
        )
    except Exception as exc:
        logger.error("List chat viewers failed: %s", exc, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to list chat viewers",
            response=response,
        )


@router.delete(
    "/{session_id}/shares/{viewer_user_id}",
    status_code=204,
    response_model=None,
    tags=["chats"],
    summary="Revoke read-only access to a chat session",
)
async def revoke_chat_viewer(
    response: FastAPIResponse,
    session_id: UUID,
    viewer_user_id: UUID,
    service: ChatSessionService = Depends(get_chat_session_service),
    _rl: None = Depends(rate_limit_user_write),
):
    """Idempotently revoke one viewer from a chat owned by the caller."""
    try:
        await service.revoke_viewer(
            session_id=session_id,
            viewer_user_id=viewer_user_id,
        )
        return Response.no_content_response()
    except ValueError:
        return Response.not_found_error_response(
            _SHARE_NOT_FOUND_MESSAGE,
            response=response,
        )
    except Exception as exc:
        logger.error("Revoke chat viewer failed: %s", exc, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to revoke chat viewer",
            response=response,
        )
