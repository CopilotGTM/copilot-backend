"""Compatibility facade for legacy deal note callers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMActivity
from src.infra.utils.logger import get_logger
from src.service.crm.notes.contracts import (
    CreateUserNoteResult,
    NotesServiceError,
    NoteWriteContext,
    ParticipantInfo,
)
from src.service.crm.notes.insight_dispatch_service import NotesInsightDispatchService
from src.service.crm.notes.read_service import NotesReadService
from src.service.crm.notes.write_service import NotesWriteService

logger = get_logger("deal_notes_service")

DealNotesServiceError = NotesServiceError


class DealNotesService:
    """Compatibility facade; use notes read/write/dispatch services for new code."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self._reader = NotesReadService(org_session, organisation_id)
        self._writer = NotesWriteService(org_session, organisation_id)
        self._dispatcher = NotesInsightDispatchService(org_session, organisation_id)

    async def close(self) -> None:
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("DealNotesService close exception", exc_info=True)

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def get_deal_notes(
        self,
        deal_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        sort_order: str = "desc",
    ) -> tuple[list[CRMActivity], str | None, int, list[ParticipantInfo]]:
        return await self._reader.get_deal_notes(
            deal_id=deal_id,
            limit=limit,
            cursor=cursor,
            sort_order=sort_order,
        )

    async def create_user_note(
        self,
        deal_id: UUID,
        content: str,
        subject: str | None,
        user_id: UUID,
        user_name: str | None,
        user_email: str | None,
        created_via: str = "api",
    ) -> CreateUserNoteResult:
        context = NoteWriteContext(
            source=created_via,
            actor_user_id=user_id,
            actor_name=user_name,
            actor_email=user_email,
            created_via=created_via,
        )
        return await self._writer.create_user_note(
            deal_id=deal_id,
            content=content,
            subject=subject,
            context=context,
        )

    async def update_user_note(
        self,
        deal_id: UUID,
        note_id: UUID,
        payload: dict[str, Any],
    ) -> CRMActivity:
        return await self._writer.update_user_note(
            deal_id=deal_id, note_id=note_id, payload=payload
        )

    async def delete_user_note(self, deal_id: UUID, note_id: UUID) -> CRMActivity:
        return await self._writer.delete_user_note(deal_id=deal_id, note_id=note_id)

    async def trigger_deal_insight_pipeline(
        self,
        deal_id: UUID,
        user_id: UUID,
        trigger_source: str = "note_creation",
        trigger_source_id: UUID | None = None,
    ) -> UUID | None:
        return await self._dispatcher.trigger_deal_insight_pipeline(
            deal_id=deal_id,
            user_id=user_id,
            trigger_source=trigger_source,
            trigger_source_id=trigger_source_id,
        )

    def determine_note_source(self, note: CRMActivity) -> str:
        return self._reader.determine_note_source(note)
