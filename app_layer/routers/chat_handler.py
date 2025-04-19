from uuid import UUID
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from core.dto.authentication import Authentication
from core.database import get_master_session
from app_layer.utils.logger import get_logger
from models.chat_models import CreateChat, DeleteChat, ChatSessionResponse
from models.chat_db_models import ChatSession as ChatSessionDB

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


@chat_router.get("/get_chats", response_model=List[ChatSessionResponse])
def get_chats(
    auth_data: Dict[str, Any] = Depends(auth.verify_session),
    master_session: Session = Depends(get_master_session),
) -> List[ChatSessionDB]:
    """
    Retrieve all non-deleted chat sessions for the authenticated user/org,
    ordered by most recent.
    """
    logger.info("Fetching chat sessions for user/org")
    try:
        user_id = UUID(auth_data.get("user_id"))
        organisation_id = UUID(auth_data.get("organisation_id"))
    except (ValueError, TypeError):
        logger.error("Invalid UUID in auth payload: %s", auth_data)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication payload",
        )

    statement = (
        select(ChatSessionDB)
        .where(
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.organisation_id == organisation_id,
            ChatSessionDB.is_deleted == False,
        )
        .order_by(ChatSessionDB.created_at.desc())
    )
    statement = statement.execution_options(postgresql_literal_binds=True)
    results = master_session.exec(statement)
    chats = results.all()
    logger.info("Fetched %d chat sessions for user/org", len(chats))
    return chats

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
