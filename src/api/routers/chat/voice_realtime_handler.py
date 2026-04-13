"""Voice realtime chat persistence endpoints.

Handles persisting voice conversation turns as ChatMessages within
the existing chat system.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from fastapi import Response as FastAPIResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.chat_message_models import ChatMessage, ChatMessageRole
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.infra.utils.logger import get_logger

logger = get_logger("voice_realtime_handler")
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class VoiceTurnEntry(BaseModel):
    """A single completed voice turn (user spoke or assistant responded)."""

    realtime_item_id: str
    speaker: Literal["user", "assistant"]
    text: str
    turn_index: int


class PersistVoiceTurnsRequest(BaseModel):
    """Batch of voice turns to persist as ChatMessages."""

    chat_session_id: UUID
    turns: list[VoiceTurnEntry]


class PersistVoiceTurnsResponse(BaseModel):
    persisted_count: int
    message_ids: list[UUID]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/voice/turns",
    response_model=GenericResponse[PersistVoiceTurnsResponse],
    tags=["voice_realtime"],
    summary="Persist completed voice turns as chat messages",
)
async def persist_voice_turns(
    response: FastAPIResponse,
    payload: PersistVoiceTurnsRequest = Body(...),
    user_ids: UserIDs = Depends(get_current_user),
    db: AsyncSession = Depends(get_org_async_session),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[PersistVoiceTurnsResponse]:
    """Persist completed voice turns (user transcription + Kai response) as ChatMessages."""
    try:
        # Verify the session belongs to the caller and is a VOICE_CHAT.
        result = await db.execute(
            select(ChatSession).where(
                col(ChatSession.session_id) == payload.chat_session_id,
                col(ChatSession.user_id) == user_ids.user_id,
                col(ChatSession.organisation_id) == user_ids.organisation_id,
                col(ChatSession.chat_type) == ChatType.VOICE_CHAT,
                col(ChatSession.is_deleted) == False,  # noqa: E712
            )
        )
        chat_session = result.scalars().first()

        if not chat_session:
            return Response.error_response(
                status_code=404,
                message="Voice chat session not found.",
                response=response,
            )

        # Dedup: find existing realtime_item_ids in this session.
        existing_result = await db.execute(
            select(ChatMessage.content["realtime_item_id"].as_string()).where(
                col(ChatMessage.session_id) == payload.chat_session_id,
            )
        )
        existing_item_ids = {row for row in existing_result.scalars().all() if row}

        # Create messages for turns that haven't been persisted yet.
        created_ids: list[UUID] = []
        first_user_text: str | None = None

        for turn in payload.turns:
            if turn.realtime_item_id in existing_item_ids:
                continue

            role = ChatMessageRole.USER if turn.speaker == "user" else ChatMessageRole.ASSISTANT
            msg = ChatMessage(
                session_id=payload.chat_session_id,
                role=role,
                content={
                    "text": turn.text,
                    "source": "voice_transcription",
                    "realtime_item_id": turn.realtime_item_id,
                },
                extra={"turn_index": turn.turn_index},
            )
            db.add(msg)
            await db.flush()
            created_ids.append(msg.message_id)
            existing_item_ids.add(turn.realtime_item_id)

            if turn.speaker == "user" and first_user_text is None:
                first_user_text = turn.text

        # Auto-title the session from the first user utterance.
        if first_user_text and chat_session.title == "Voice Session":
            chat_session.title = first_user_text[:60].strip()
            if len(first_user_text) > 60:
                chat_session.title += "..."

        await db.commit()

        return Response.success_response(
            message=f"Persisted {len(created_ids)} voice turns.",
            data=PersistVoiceTurnsResponse(
                persisted_count=len(created_ids),
                message_ids=created_ids,
            ),
            response=response,
        )
    except Exception:
        logger.exception(
            "Failed to persist voice turns",
            extra={"chat_session_id": str(payload.chat_session_id)},
        )
        await db.rollback()
        return Response.internal_server_error_response(
            message="Failed to persist voice turns.",
            response=response,
        )
