# app_layer/routers/general_chat.py
from uuid import UUID
from datetime import datetime
from typing import List, Dict, Optional
from app_layer.utils.logger import get_logger

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from core.dto.authentication import Authentication
from models.chat_models import CreateChat
from models.chat_db_models import ChatSession, ChatMessage
from core.app_state import fastapp
import logging
from core.database import get_master_session

auth = Authentication()
chat_router = APIRouter()
logger = get_logger()


from sqlmodel import SQLModel, Field


class CreateChat(SQLModel):
    title: str


@chat_router.post("/create_chat", response_model=ChatSession)
def create_chat(
    chat_in: CreateChat,
    master_session: Session = Depends(get_master_session),  # ← here
):

    logger.info("Creating a new chat with title")
    if not chat_in.title:
        logger.warning("Attempted to create a chat with an empty title")
        raise HTTPException(400, "Title cannot be null or empty")

    chat = ChatSession(
        # assagin uuids to user_id and organisation_id
        user_id=UUID("12345678-1234-1234-1234-123456789012"),
        organisation_id=UUID("87654321-4321-4321-4321-210987654321"),
        title=chat_in.title,
    )
    master_session.add(chat)
    master_session.commit()
    master_session.refresh(chat)
    logger.info(f"Chat created successfully with ID: {chat.id}")
    return chat


# @chat_router.get("/get_chats", response_model=List[ChatSession])
# def list_chats(user_id: str, session: Session = Depends(get_db)):
#     return session.exec(
#         select(ChatSession)
#         .where(ChatSession.user_id == user_id)
#         .where(ChatSession.is_deleted == False)
#         .order_by(ChatSession.created_at.desc())
#     ).all()

# @chat_router.post("/{session_id}/messages", response_model=ChatMessage)
# def add_msg(
#     session_id: UUID,
#     role: str,
#     content: str,
#     extra: Dict = {},
#     session: Session = Depends(get_db),
# ):
#     chat = session.get(ChatSession, session_id)
#     if not chat or chat.is_deleted:
#         raise HTTPException(404, f"Chat {session_id} not found")
#     msg = ChatMessage(
#         session_id=session_id,
#         role=role,
#         content=content,
#         timestamp=datetime.utcnow(),
#         extra=extra,
#     )
#     session.add(msg)
#     session.commit()
#     session.refresh(msg)
#     return msg

# @chat_router.get("/{session_id}/messages", response_model=List[ChatMessage])
# def get_msgs(session_id: UUID, session: Session = Depends(get_db)):
#     chat = session.get(ChatSession, session_id)
#     if not chat or chat.is_deleted:
#         raise HTTPException(404, f"Chat {session_id} not found")
#     return session.exec(
#         select(ChatMessage)
#         .where(ChatMessage.session_id == session_id)
#         .order_by(ChatMessage.timestamp)
#     ).all()
