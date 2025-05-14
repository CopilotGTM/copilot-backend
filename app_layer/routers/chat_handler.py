from uuid import UUID
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlmodel import Session, select
from sqlalchemy import or_, and_

from core.dto.authentication import Authentication
from core.database import get_master_session
from app_layer.utils.logger import get_logger
from models.chat_models import CreateChat, DeleteChat, ChatSessionResponse, Pagination, PaginationChatMessages
from models.chat_db_models import ChatSession as ChatSessionDB
from models.chat_db_models import ChatMessage as ChatMessageDB
from models.http_response import Response
from app_layer.utils.cursor import encode_cursor, decode_cursor

auth = Authentication()
logger = get_logger()
chat_router = APIRouter()


@chat_router.post("/create_chat", response_model=ChatSessionResponse)
def create_chat(
    chat_in: CreateChat,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> ChatSessionDB:
    """
    Create a new chat session for the authenticated user/org.
    """
    logger.info("Creating a new chat with title=%s", chat_in.title)
    if not chat_in.title.strip():
        logger.warning("Attempted to create a chat with an empty title")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Title cannot be null or empty",
        )

    # Extract and validate user/org IDs from the decoded JWT payload
    try:
        user_id = UUID(auth_data.get("user_id"))
        organisation_id = UUID(auth_data.get("organisation_id"))
    except (ValueError, TypeError):
        logger.error("Invalid UUID in auth payload: %s", auth_data)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication payload",
        )

    # Persist new chat session
    chat = ChatSessionDB(
        user_id=user_id,
        organisation_id=organisation_id,
        title=chat_in.title,
    )
    master_session.add(chat)
    master_session.commit()
    master_session.refresh(chat)

    logger.info("Chat created successfully with session_id=%s", chat.session_id)
    return chat


@chat_router.get("/get_chats", response_model=Pagination)
def get_chats(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(
        None,
        description="Opaque cursor returned by the previous call (omit for first page)",
    ),
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
):
    """Return the next slice of chats for the authenticated user/org."""
    try:
        user_id = UUID(auth_data["user_id"])
        organisation_id = UUID(auth_data["organisation_id"])

        filters = [
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.organisation_id == organisation_id,
            ChatSessionDB.is_deleted.is_(False),
        ]

        # key-set pagination (created_at DESC, session_id DESC)
        if cursor:
            ts, sid = decode_cursor(cursor)
            filters.append(
                or_(
                    ChatSessionDB.created_at < ts,
                    and_(
                        ChatSessionDB.created_at == ts,
                        ChatSessionDB.session_id < sid,
                    ),
                )
            )

        stmt = (
            select(ChatSessionDB)
            .where(*filters)
            .order_by(ChatSessionDB.created_at.desc(), ChatSessionDB.session_id.desc())
            .limit(limit + 1)  # grab one extra row to detect “more”
        )

        rows: List[ChatSessionDB] = master_session.exec(stmt).all()
        has_more = len(rows) == limit + 1
        items = rows[:limit]

        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].session_id) if has_more else None
        )
        logger.info("Data Success")
        payload = Pagination[ChatSessionResponse](items=items, cursor=next_cursor)
        return payload
    except Exception as error:
        logger.error(f"Unable to retrieve chat List user- {user_id}. Error - {error}")
        return Response.internal_server_error_response("Unable to retrieve chat List", data=None)
 

@chat_router.delete("/delete_chat", response_model=ChatSessionResponse)
def delete_chat(
    delete_in: DeleteChat,
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> ChatSessionDB:
    """
    Soft-delete a chat session by marking is_deleted = True.
    """
    # Extract session_id from request model
    session_id = delete_in.session_id

    # Validate user/org IDs
    try:
        user_id = UUID(auth_data.get("user_id"))
        organisation_id = UUID(auth_data.get("organisation_id"))
    except (ValueError, TypeError):
        logger.error("Invalid UUID in auth payload: %s", auth_data)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication payload",
        )

    # Fetch the chat session to delete
    statement = select(ChatSessionDB).where(
        ChatSessionDB.session_id == session_id,
        ChatSessionDB.user_id == user_id,
        ChatSessionDB.organisation_id == organisation_id,
        ChatSessionDB.is_deleted == False,
    )
    chat = master_session.exec(statement).one_or_none()

    if not chat:
        logger.warning("Chat session not found or already deleted: %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat session not found",
        )

    # Soft-delete
    chat.is_deleted = True
    master_session.add(chat)
    master_session.commit()
    master_session.refresh(chat)

    logger.info("Chat session soft-deleted: %s", session_id)
    return chat


@chat_router.get("/get_chat", response_model=PaginationChatMessages)
def get_chat_by_id(
    session_id: str,
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(
        None,
        description="Opaque cursor returned by the previous call (omit for first page)",
    ),
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> Dict[str, Any]:
    """
    Retrieve a specific chat session by its ID for the authenticated user/org,
    along with its messages as a list of links.
    """
    logger.info("Begin fetching chat session by ID: %s", session_id)

    try:
        # Extracting User Details
        user_id = UUID(auth_data.get("user_id"))
        organisation_id = UUID(auth_data.get("organisation_id"))

        # Fetch chat session
        logger.info("Querying for chat session: %s", session_id)
        statement = select(ChatSessionDB).where(
            ChatSessionDB.session_id == session_id,
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.organisation_id == organisation_id,
            ChatSessionDB.is_deleted == False,
        )
        chat = master_session.exec(statement).one_or_none()

        if not chat:
            logger.warning(
                "Chat session not found: session_id=%s, user_id=%s, organisation_id=%s",
                session_id,
                user_id,
                organisation_id,
            )
            return Response.not_found_error_response("Chat Session does not exist", data=None)

        filters = [ChatMessageDB.session_id == session_id]
        if cursor:
            ts, mid = decode_cursor(cursor)
            filters.append(
                or_(
                    ChatMessageDB.timestamp > ts,
                    and_(
                        ChatMessageDB.timestamp == ts,
                        ChatMessageDB.message_id > mid,
                    ),
                )
            )

        msg_stmt = (
            select(ChatMessageDB)
            .where(*filters)
            .order_by(ChatMessageDB.timestamp.asc(), ChatMessageDB.message_id.asc())
            .limit(limit + 1)
        )
        messages = master_session.exec(msg_stmt).all()
        has_more = len(messages) == limit + 1
        page_items = messages[:limit]

        next_cursor = (
            encode_cursor(page_items[-1].timestamp, page_items[-1].session_id) if has_more else None
        )

        messages_list = []
        for message in messages:
            message_dict = {
                "message_id": str(message.message_id),
                "content": message.content,
                "timestamp": message.timestamp,
                "role": message.role,
                "message_type": message.message_type,
                "message_version": message.message_version,
                "attachments": message.attachments,
                "state": ""
            }
            if message.message_type=="TECHNICAL_DECK" and message.role=='assistant':
                message_dict["state"] = message.technical_deck_details["state"]
            
            messages_list.append(message_dict)

        chat_response = {
            "session_id": str(chat.session_id),
            "title": chat.title,
            "created_at": chat.created_at.isoformat(),
            "updated_at": chat.updated_at.isoformat(),
            "messages": messages_list,
        }
        payload = PaginationChatMessages(chat=chat_response, cursor=next_cursor)
        logger.info("Successfully fetched chat session: %s", session_id)
        return payload
    except Exception as error:
        logger.error(f"Unable to retrieve chat session. Error - {error}")
        return Response.internal_server_error_response("Unable to retrieve chat session", data=None)
