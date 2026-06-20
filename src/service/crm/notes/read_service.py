"""Read-side deal note service for CRM activity notes."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.service.crm.notes.contracts import NotesServiceError, ParticipantInfo

logger = get_logger("notes_read_service")


class NotesReadService:
    """Read operations for deal notes and note mention participants."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("NotesReadService close exception", exc_info=True)

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
        """Retrieve org-scoped notes for one deal with cursor pagination."""
        deal = await self._verify_deal_access(deal_id)

        c = cast(Any, CRMActivity).__table__.c
        sort_ts = func.coalesce(c.source_created_at, c.created_at)
        base_where = and_(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            c.activity_type == CRMActivityType.NOTE.value,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )

        count_stmt = select(func.count()).select_from(CRMActivity).where(base_where)
        count_result = await self.org_session.execute(count_stmt)
        total_count = count_result.scalar() or 0

        stmt = select(CRMActivity).where(base_where)
        if cursor:
            try:
                ts, activity_id = decode_cursor(cursor)
            except Exception as exc:
                raise NotesServiceError("Invalid cursor", status_code=400) from exc

            if sort_order == "desc":
                stmt = stmt.where(
                    or_(sort_ts < ts, and_(sort_ts == ts, c.activity_id < activity_id))
                )
            else:
                stmt = stmt.where(
                    or_(sort_ts > ts, and_(sort_ts == ts, c.activity_id > activity_id))
                )

        if sort_order == "desc":
            stmt = stmt.order_by(sort_ts.desc(), c.activity_id.desc())
        else:
            stmt = stmt.order_by(sort_ts.asc(), c.activity_id.asc())

        result = await self.org_session.execute(stmt.limit(limit + 1))
        rows: list[CRMActivity] = list(result.scalars().all())
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = encode_cursor(last.source_created_at or last.created_at, last.activity_id)

        participants = await self._get_deal_participants(deal_id, deal)
        return items, next_cursor, total_count, participants

    async def _get_deal_participants(self, deal_id: UUID, deal: CRMDeal) -> list[ParticipantInfo]:
        """Fetch owner and approved contact participants for note mentions."""
        participants: list[ParticipantInfo] = []
        seen_emails: set[str] = set()

        if deal.owner_email:
            seen_emails.add(deal.owner_email.lower())
            participants.append(ParticipantInfo(email=deal.owner_email, name=deal.owner_name))

        participants_stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
        )
        result = await self.org_session.execute(participants_stmt)
        for participant in result.scalars().all():
            if not participant.related_email:
                continue
            email_lower = participant.related_email.lower()
            if email_lower in seen_emails:
                continue
            seen_emails.add(email_lower)
            participants.append(
                ParticipantInfo(email=participant.related_email, name=participant.related_name)
            )

        return participants

    async def _verify_deal_access(self, deal_id: UUID) -> CRMDeal:
        """Load the deal using organisation_id as the tenant boundary."""
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

    def determine_note_source(self, note: CRMActivity) -> str:
        """Return the UI source label for user-created or provider-synced notes."""
        if note.integration_id is None:
            return "user"

        properties: dict[str, Any] = note.properties or {}
        provider_raw = properties.get("provider")
        if isinstance(provider_raw, str):
            provider = provider_raw.strip().lower()
            if provider:
                return provider

        return "hubspot"
