"""Write-side deal note service for internal user-created notes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivitySource,
    CRMActivityType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.crm.notes.contracts import (
    CreateUserNoteResult,
    NotesServiceError,
    NoteWriteContext,
)

logger = get_logger("notes_write_service")


class NotesWriteService:
    """Durable user-note mutations; callers own commit and rollback."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("NotesWriteService close exception", exc_info=True)

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def create_user_note(
        self,
        deal_id: UUID,
        content: str,
        subject: str | None,
        context: NoteWriteContext,
    ) -> CreateUserNoteResult:
        """Create or reuse an exact active user-created note for one org-owned deal."""
        await self._verify_deal_access(deal_id)

        existing_note = await self._find_existing_user_note(
            deal_id=deal_id,
            content=content,
            subject=subject,
        )
        if existing_note:
            logger.info(
                "Duplicate user note ignored for deal %s by user %s; existing note %s reused",
                deal_id,
                context.actor_user_id,
                existing_note.activity_id,
            )
            return CreateUserNoteResult(note=existing_note, created_new=False)

        now = datetime.now(UTC)
        note = CRMActivity(
            activity_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=None,
            activity_type=CRMActivityType.NOTE.value,
            subject=subject,
            body=content,
            deal_id=deal_id,
            owner_name=context.actor_name,
            owner_email=context.actor_email,
            source=CRMActivitySource.MANUAL.value,
            created_by_user_id=context.actor_user_id,
            source_created_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "is_user_created": True,
                "created_via": context.created_via,
            },
        )

        self.org_session.add(note)
        await self.org_session.flush()
        await self.org_session.refresh(note)

        logger.info(
            "Created user note %s for deal %s by user %s",
            note.activity_id,
            deal_id,
            context.actor_user_id,
        )

        return CreateUserNoteResult(note=note, created_new=True)

    async def update_user_note(
        self,
        deal_id: UUID,
        note_id: UUID,
        payload: dict[str, Any],
    ) -> CRMActivity:
        """Update mutable fields on a user-created note."""
        if not payload:
            raise NotesServiceError("No fields to update", status_code=400)

        note = await self._load_user_note(deal_id, note_id)

        updated = False
        if "content" in payload:
            if payload["content"] is None:
                raise NotesServiceError("Content cannot be null", status_code=400)
            note.body = payload["content"]
            updated = True
        if "subject" in payload:
            note.subject = payload["subject"]
            updated = True

        if not updated:
            raise NotesServiceError("No fields to update", status_code=400)

        now = datetime.now(UTC)
        note.source_modified_at = now
        note.synced_at = now
        note.last_seen_at = now
        properties = note.properties or {}
        properties["updated_via"] = "api"
        note.properties = properties

        await self.org_session.flush()
        await self.org_session.refresh(note)

        return note

    async def delete_user_note(self, deal_id: UUID, note_id: UUID) -> CRMActivity:
        """Soft-delete a user-created note without touching provider-owned rows."""
        note = await self._load_user_note(deal_id, note_id)

        now = datetime.now(UTC)
        note.is_deleted = True
        note.source_modified_at = now
        note.synced_at = now
        note.last_seen_at = now
        properties = note.properties or {}
        properties["deleted_via"] = "api"
        note.properties = properties

        await self.org_session.flush()
        await self.org_session.refresh(note)

        return note

    async def _verify_deal_access(self, deal_id: UUID) -> CRMDeal:
        c = cast(Any, CRMDeal).__table__.c
        stmt = select(CRMDeal).where(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
        )
        result = await self.org_session.execute(stmt)
        deal = result.scalar_one_or_none()
        if not deal:
            raise NotesServiceError(f"Deal {deal_id} not found or access denied", status_code=404)
        return deal

    async def _load_user_note(self, deal_id: UUID, note_id: UUID) -> CRMActivity:
        note = await self.org_session.get(CRMActivity, note_id)
        if not note or note.organisation_id != self.organisation_id:
            raise NotesServiceError("Note not found", status_code=404)
        if note.deal_id != deal_id:
            raise NotesServiceError("Note not found", status_code=404)
        if note.is_deleted:
            raise NotesServiceError("Note not found", status_code=404)
        if note.activity_type != CRMActivityType.NOTE.value:
            raise NotesServiceError("Note not found", status_code=404)
        if note.integration_id is not None:
            raise NotesServiceError("External CRM notes are read-only", status_code=409)
        return note

    async def _find_existing_user_note(
        self,
        deal_id: UUID,
        content: str,
        subject: str | None,
    ) -> CRMActivity | None:
        """Find an active internal note with identical deal/content/subject."""
        c = cast(Any, CRMActivity).__table__.c
        stmt = select(CRMActivity).where(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            c.activity_type == CRMActivityType.NOTE.value,
            c.integration_id.is_(None),
            c.body == content,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )
        stmt = stmt.where(c.subject.is_(None) if subject is None else c.subject == subject)

        sort_ts = func.coalesce(c.source_created_at, c.created_at)
        result = await self.org_session.execute(
            stmt.order_by(sort_ts.desc(), c.activity_id.desc()).limit(1)
        )
        return result.scalars().first()
