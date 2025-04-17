# app_layer/routers/general_chat.py
from uuid import UUID
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from core.dto.authentication import Authentication

from models.chat import ChatSession, ChatMessage
from core.app_state import fastapp

auth = Authentication()
router = APIRouter(dependencies=[Depends(auth.verify_session)])

get_db = lambda: fastapp.state.master_session()

@router.post("/create_chat", response_model=ChatSession)
def create_chat(
    title: str,
    session: Session = Depends(get_db),
    current=Depends(auth.verify_session),
):
    if not title:
        raise HTTPException(400, "Title cannot be null or empty")

    chat = ChatSession(
        user_id=current["user_id"],
        title=title,
    )
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat

@router.get("/", response_model=List[ChatSession])
def list_chats(user_id: str, session: Session = Depends(get_db)):
    return session.exec(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .where(ChatSession.is_deleted == False)
        .order_by(ChatSession.created_at.desc())
    ).all()

@router.post("/{session_id}/messages", response_model=ChatMessage)
def add_msg(
    session_id: UUID,
    role: str,
    content: str,
    extra: Dict = {},
    session: Session = Depends(get_db),
):
    chat = session.get(ChatSession, session_id)
    if not chat or chat.is_deleted:
        raise HTTPException(404, f"Chat {session_id} not found")
    msg = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        timestamp=datetime.utcnow(),
        extra=extra,
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg

@router.get("/{session_id}/messages", response_model=List[ChatMessage])
def get_msgs(session_id: UUID, session: Session = Depends(get_db)):
    chat = session.get(ChatSession, session_id)
    if not chat or chat.is_deleted:
        raise HTTPException(404, f"Chat {session_id} not found")
    return session.exec(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.timestamp)
    ).all()
