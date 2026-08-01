"""Read-only CRM lead service."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadSourceType,
    CRMLeadStatus,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMRecordAccessError,
    LeadAccessLevel,
    LeadAccessPolicy,
)
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)
from src.service.crm.utils.identity import is_valid_email, normalize_spoken_email_query

logger = get_logger("lead_read_service")


class LeadReadService:
    """Read CRM leads and participants for one organisation."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID | None = None,
        master_session: AsyncSession | None = None,
        access_actor: CRMAccessActor | None = None,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.master_session = master_session
        self.access_actor = access_actor
        self._access = LeadAccessPolicy(org_session, organisation_id, access_actor)

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
            self._access.visible_lead_predicate(),
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

    async def search_leads_for_agent(
        self,
        *,
        q: str,
        limit: int = 20,
        status: CRMLeadStatus | None = None,
        owner_id: UUID | None = None,
        source_type: CRMLeadSourceType | None = None,
    ) -> list[dict[str, Any]]:
        """Return fuzzy lead suggestions after the agent's literal search misses.

        This method is agent-only so picker and HTTP list semantics remain
        literal. Email matches expose their field and score to let the agent ask
        for confirmation instead of treating a close address as authoritative.
        """
        query = normalize_spoken_email_query(q)
        if not query:
            return []

        name_score = func.similarity(func.coalesce(col(CRMLead.full_name), ""), query)
        company_score = func.similarity(func.coalesce(col(CRMLead.company_name), ""), query)
        domain_score = func.similarity(func.coalesce(col(CRMLead.company_domain), ""), query)
        email_score = func.similarity(func.coalesce(col(CRMLead.email), ""), query)
        match_score: ColumnElement[Any] = func.greatest(
            name_score, email_score, company_score, domain_score
        )
        match_field: ColumnElement[Any] = case(
            (name_score >= func.greatest(email_score, company_score, domain_score), "full_name"),
            (email_score >= func.greatest(company_score, domain_score), "email"),
            (company_score >= domain_score, "company_name"),
            else_="company_domain",
        )

        if is_valid_email(query):
            query_local, query_domain = query.split("@", 1)
            stored_email = func.lower(func.coalesce(col(CRMLead.email), ""))
            local_score = func.similarity(func.split_part(stored_email, "@", 1), query_local)
            stored_domain_score = func.similarity(
                func.split_part(stored_email, "@", 2),
                query_domain,
            )
            match_score = local_score * 0.75 + stored_domain_score * 0.25
            match_field = literal("email")
            fuzzy_filter: ColumnElement[bool] = (local_score >= 0.40) & (match_score >= 0.55)
        else:
            fuzzy_filter = match_score >= 0.40

        stmt = select(
            CRMLead,
            match_score.label("score"),
            match_field.label("match_field"),
        ).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.is_deleted).is_(False),
            self._access.visible_lead_predicate(),
            fuzzy_filter,
        )
        if status is not None:
            stmt = stmt.where(col(CRMLead.status) == status.value)
        if owner_id is not None:
            stmt = stmt.where(col(CRMLead.owner_id) == owner_id)
        if source_type is not None:
            stmt = stmt.where(col(CRMLead.source_type) == source_type.value)

        rows = (
            await self.org_session.execute(
                stmt.order_by(match_score.desc(), col(CRMLead.lead_id).asc()).limit(
                    max(1, min(limit, 20))
                )
            )
        ).all()
        return [
            {
                "lead": row[0],
                "match": {
                    "field": row.match_field,
                    "type": "fuzzy",
                    "score": float(row.score or 0.0),
                },
            }
            for row in rows
        ]

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

    async def _get_lead_or_404(
        self,
        lead_id: UUID,
        level: LeadAccessLevel = "view",
    ) -> CRMLead:
        try:
            lead, _decision = await self._access.require_lead(lead_id, level)
        except CRMRecordAccessError as exc:
            raise LeadServiceError(exc.message, status_code=exc.status_code) from exc
        return cast(CRMLead, lead)

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return value.value if hasattr(value, "value") else value
