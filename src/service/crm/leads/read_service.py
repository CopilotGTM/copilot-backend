"""Read-only CRM lead service."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadSourceType,
    CRMLeadStatus,
)
from src.infra.utils.logger import get_logger
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)

logger = get_logger("lead_read_service")


class LeadReadService:
    """Read CRM leads and participants for one organisation."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID | None = None,
        master_session: AsyncSession | None = None,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.master_session = master_session

    async def close(self) -> None:
        """Clean up any resources."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("LeadReadService close exception", exc_info=True)

    async def list_leads(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        status: CRMLeadStatus | None = None,
        owner_id: UUID | None = None,
        source_type: CRMLeadSourceType | None = None,
    ) -> tuple[list[CRMLead], int]:
        """List leads with org-scoped filters and pagination."""
        if page < 1:
            raise LeadServiceError("Page must be >= 1", status_code=400)

        base = select(CRMLead).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.is_deleted).is_(False),
        )

        if q:
            like_expr = f"%{q.lower()}%"
            base = base.where(
                col(CRMLead.full_name).ilike(like_expr)
                | col(CRMLead.email).ilike(like_expr)
                | col(CRMLead.company_name).ilike(like_expr)
                | col(CRMLead.company_domain).ilike(like_expr)
            )
        if status is not None:
            base = base.where(col(CRMLead.status) == status.value)
        if owner_id is not None:
            base = base.where(col(CRMLead.owner_id) == owner_id)
        if source_type is not None:
            base = base.where(col(CRMLead.source_type) == source_type.value)

        count_stmt = select(func.count()).select_from(base.subquery())
        count_res = await self.org_session.execute(count_stmt)
        total_count = count_res.scalar() or 0

        offset = (page - 1) * limit
        stmt = (
            base.order_by(col(CRMLead.captured_at).desc(), col(CRMLead.lead_id).desc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self.org_session.execute(stmt)).scalars().all()
        return list(rows), total_count

    async def get_lead(self, lead_id: UUID) -> tuple[CRMLead, list[CRMLeadParticipant]]:
        """Return a lead and its active participants."""
        lead = await self._get_lead_or_404(lead_id)
        participants_stmt = (
            select(CRMLeadParticipant)
            .where(
                col(CRMLeadParticipant.organisation_id) == self.organisation_id,
                col(CRMLeadParticipant.lead_id) == lead_id,
                col(CRMLeadParticipant.is_deleted).is_(False),
            )
            .order_by(
                col(CRMLeadParticipant.is_primary).desc(),
                col(CRMLeadParticipant.created_at).asc(),
            )
        )
        participants = (await self.org_session.execute(participants_stmt)).scalars().all()
        return lead, list(participants)

    async def _resolve_actor_crm_user_id(self) -> UUID | None:
        """Return a valid crm_users.user_id for audit FKs, creating it on demand."""
        if self.actor_user_id is None:
            return None
        if self.master_session is None:
            return None

        try:
            crm_user = await ensure_app_user_crm_identity(
                org_session=self.org_session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                app_user_id=self.actor_user_id,
                actor_user_id=self.actor_user_id,
            )
        except CRMUserIdentityResolutionError as exc:
            raise LeadServiceError(exc.message, exc.status_code) from exc
        return crm_user.user_id

    async def _get_lead_or_404(self, lead_id: UUID) -> CRMLead:
        stmt = select(CRMLead).where(
            col(CRMLead.lead_id) == lead_id,
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        lead = res.scalars().first()
        if not lead:
            raise LeadServiceError("Lead not found", status_code=404)
        return lead

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return value.value if hasattr(value, "value") else value
