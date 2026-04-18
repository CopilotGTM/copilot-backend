from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class VoiceTurnPersistenceServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class VoiceTurnInput:
    realtime_item_id: str
    speaker: Literal["user", "assistant"]
    text: str
    turn_index: int


@dataclass(frozen=True, slots=True)
class PersistVoiceTurnsResult:
    persisted_count: int
    message_ids: list[UUID]


class VoiceTurnPersistenceService:
    """Persist completed voice turns into the chat message store."""

    def __init__(self, *, db: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.db = db
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def persist_turns(
        self,
        *,
        chat_session_id: UUID,
        turns: list[VoiceTurnInput],
    ) -> PersistVoiceTurnsResult:
        try:
            chat_session = await self._get_owned_voice_session(chat_session_id)
            existing_item_ids = await self._get_existing_realtime_item_ids(chat_session_id)

            created_ids: list[UUID] = []
            first_user_text: str | None = None
            last_user_message_id: UUID | None = None

            for turn in turns:
                role = ChatMessageRole.USER if turn.speaker == "user" else ChatMessageRole.ASSISTANT
                realtime_item_id = turn.realtime_item_id

                if realtime_item_id in existing_item_ids:
                    continue

                message = ChatMessage(
                    session_id=chat_session_id,
                    parent_message_id=(
                        last_user_message_id if role == ChatMessageRole.ASSISTANT else None
                    ),
                    role=role,
                    message_type=ChatMessageType.GENERAL_MESSAGE,
                    content={"text": turn.text},
                    model_name=(
                        self._resolve_assistant_model_name(chat_session)
                        if role == ChatMessageRole.ASSISTANT
                        else None
                    ),
                    extra=self._build_message_extra(
                        realtime_item_id=realtime_item_id,
                        turn_index=turn.turn_index,
                        role=role,
                    ),
                )
                self.db.add(message)
                await self.db.flush()
                created_ids.append(message.message_id)
                existing_item_ids.add(realtime_item_id)

                if role == ChatMessageRole.USER:
                    last_user_message_id = message.message_id
                if role == ChatMessageRole.USER and first_user_text is None:
                    first_user_text = turn.text

            if first_user_text and chat_session.title == "Voice Session":
                chat_session.title = self._build_voice_session_title(first_user_text)

            await self.db.commit()
            return PersistVoiceTurnsResult(
                persisted_count=len(created_ids),
                message_ids=created_ids,
            )
        except VoiceTurnPersistenceServiceError:
            raise
        except Exception:
            logger.exception(
                "Failed to persist voice turns",
                extra={
                    "chat_session_id": str(chat_session_id),
                    "organisation_id": str(self.organisation_id),
                    "user_id": str(self.user_id),
                },
            )
            await self.db.rollback()
            raise

    async def _get_owned_voice_session(self, chat_session_id: UUID) -> ChatSession:
        result = await self.db.execute(
            select(ChatSession).where(
                col(ChatSession.session_id) == chat_session_id,
                col(ChatSession.user_id) == self.user_id,
                col(ChatSession.organisation_id) == self.organisation_id,
                col(ChatSession.chat_type) == ChatType.VOICE_CHAT,
                col(ChatSession.is_deleted) == False,  # noqa: E712
            )
        )
        chat_session = result.scalars().first()
        if chat_session is None:
            raise VoiceTurnPersistenceServiceError("Voice chat session not found.", status_code=404)
        return chat_session

    async def _get_existing_realtime_item_ids(self, chat_session_id: UUID) -> set[str]:
        result = await self.db.execute(
            select(
                func.coalesce(
                    ChatMessage.extra["realtime_item_id"].as_string(),
                    ChatMessage.content["realtime_item_id"].as_string(),
                )
            ).where(
                col(ChatMessage.session_id) == chat_session_id,
            )
        )
        return {item_id for item_id in result.scalars().all() if item_id}

    @staticmethod
    def _build_message_extra(
        *,
        realtime_item_id: str,
        turn_index: int,
        role: ChatMessageRole,
    ) -> dict[str, str | int]:
        extra: dict[str, str | int] = {
            "source": "voice_transcription",
            "realtime_item_id": realtime_item_id,
            "turn_index": turn_index,
            "audio_origin": "voice_realtime",
        }
        if role == ChatMessageRole.USER:
            extra["input_modality"] = "audio"
        return extra

    @staticmethod
    def _resolve_assistant_model_name(chat_session: ChatSession) -> str | None:
        properties = getattr(chat_session, "properties", None) or {}
        model_name = properties.get("model")
        return str(model_name) if model_name else None

    @staticmethod
    def _build_voice_session_title(first_user_text: str) -> str:
        title = first_user_text[:60].strip()
        if len(first_user_text) > 60:
            title += "..."
        return title


__all__ = [
    "PersistVoiceTurnsResult",
    "VoiceTurnInput",
    "VoiceTurnPersistenceService",
    "VoiceTurnPersistenceServiceError",
]
