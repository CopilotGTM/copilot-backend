"""Voice realtime chat persistence endpoints.

Handles persisting voice conversation turns as ChatMessages within
the existing chat system.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from fastapi import Response as FastAPIResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.voice_realtime.turn_persistence_service import (
    VoiceTurnInput,
    VoiceTurnPersistenceService,
    VoiceTurnPersistenceServiceError,
)

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


async def get_voice_turn_persistence_service(
    user_ids: UserIDs = Depends(get_current_user),
    db: AsyncSession = Depends(get_org_async_session),
) -> AsyncIterator[VoiceTurnPersistenceService]:
    yield VoiceTurnPersistenceService(
        db=db,
        organisation_id=user_ids.organisation_id,
        user_id=user_ids.user_id,
    )


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
    service: VoiceTurnPersistenceService = Depends(get_voice_turn_persistence_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[PersistVoiceTurnsResponse]:
    """Persist completed voice turns (user transcription + Kai response) as ChatMessages."""
    try:
        result = await service.persist_turns(
            chat_session_id=payload.chat_session_id,
            turns=[
                VoiceTurnInput(
                    realtime_item_id=turn.realtime_item_id,
                    speaker=turn.speaker,
                    text=turn.text,
                    turn_index=turn.turn_index,
                )
                for turn in payload.turns
            ],
        )

        return Response.success_response(
            message=f"Persisted {result.persisted_count} voice turns.",
            data=PersistVoiceTurnsResponse(
                persisted_count=result.persisted_count,
                message_ids=result.message_ids,
            ),
            response=response,
        )
    except VoiceTurnPersistenceServiceError as exc:
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected voice turn persistence failure")
        return Response.internal_server_error_response(
            message="Failed to persist voice turns.",
            response=response,
        )
