"""Read-only CRM contact service."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.logger import get_logger
from src.service.crm.contacts.contracts import ContactReadServiceError

logger = get_logger("contact_read_service")


class ContactReadService:
    """Tenant-scoped contact queries used by API and agent workflows."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Bind the service to one org-scoped session and immutable org boundary."""
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Close the owned org session when used as a FastAPI dependency."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("ContactReadService close exception", exc_info=True)

    @staticmethod
    def _extract_domain(email: str) -> str | None:
        """Extract lowered domain from email. Returns None if invalid."""
        if not email or "@" not in email:
            return None
        parts = email.rsplit("@", 1)
        domain = parts[1].strip().lower()
        return domain if domain else None

    @staticmethod
    def _map_source(integration_type: str | None) -> str:
        """Map an integration_type to the user-facing source label."""
        if not integration_type or integration_type == IntegrationType.INTERNAL_CRM.value:
            return "manual"
        return integration_type

    async def resolve_source_for_contact(self, integration_id: UUID) -> str:
        """Look up the integration type for a single contact and return its source label."""
        stmt = select(col(KnowledgeIntegration.integration_type)).where(
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        res = await self.org_session.execute(stmt)
        integration_type = res.scalar()
        return self._map_source(integration_type)

    async def list_contacts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        company_id: UUID | None = None,
    ) -> tuple[list[dict], int]:
        """Paginated contacts with deal_count, source, and company_id annotations."""
        if page < 1:
            raise ContactReadServiceError("Page must be >= 1", status_code=400)

        contact_participant_match = or_(
            col(CRMDealParticipant.related_local_id) == col(CRMContact.contact_id),
            func.lower(func.trim(col(CRMDealParticipant.related_email)))
            == func.lower(func.trim(col(CRMContact.email))),
        )

        deal_count_sub = (
            select(func.count(col(CRMDealParticipant.participant_id)))
            .where(
                contact_participant_match,
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted).is_(False),
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
            )
            .correlate(CRMContact)
            .scalar_subquery()
        )

        base = select(CRMContact, deal_count_sub.label("deal_count")).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )

        if q:
            like_expr = f"%{q.lower()}%"
            base = base.where(
                col(CRMContact.full_name).ilike(like_expr)
                | col(CRMContact.email).ilike(like_expr)
                | col(CRMContact.company_name).ilike(like_expr)
            )

        if company_id is not None:
            company_stmt = select(col(CRMCompany.domain)).where(
                col(CRMCompany.company_id) == company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            company_res = await self.org_session.execute(company_stmt)
            company_domain = company_res.scalar()
            if company_domain:
                base = base.where(
                    or_(
                        col(CRMContact.company_id) == company_id,
                        func.split_part(col(CRMContact.email), "@", 2).ilike(company_domain),
                    )
                )
            else:
                base = base.where(col(CRMContact.company_id) == company_id)

        count_stmt = select(func.count()).select_from(base.subquery())
        count_res = await self.org_session.execute(count_stmt)
        total_count = count_res.scalar() or 0

        stmt = (
            base.order_by(
                col(CRMContact.created_at).desc(),
                col(CRMContact.contact_id).desc(),
            )
            .offset((page - 1) * limit)
            .limit(limit)
        )
        rows = (await self.org_session.execute(stmt)).all()

        integration_ids = {row.CRMContact.integration_id for row in rows}
        integration_map: dict[UUID, str] = {}
        if integration_ids:
            int_stmt = select(
                col(KnowledgeIntegration.integration_id),
                col(KnowledgeIntegration.integration_type),
            ).where(col(KnowledgeIntegration.integration_id).in_(list(integration_ids)))
            int_res = await self.org_session.execute(int_stmt)
            for int_id, int_type in int_res.all():
                integration_map[int_id] = int_type

        domains = {
            self._extract_domain(row.CRMContact.email) for row in rows if row.CRMContact.email
        }
        domain_to_company_id: dict[str, UUID] = {}
        if domains:
            co_stmt = select(col(CRMCompany.domain), col(CRMCompany.company_id)).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
                col(CRMCompany.domain).in_([d for d in domains if d]),
            )
            co_res = await self.org_session.execute(co_stmt)
            for domain, cid in co_res.all():
                if domain:
                    domain_to_company_id[domain.lower()] = cid

        result: list[dict] = []
        for row in rows:
            contact: CRMContact = row.CRMContact
            email_domain = self._extract_domain(contact.email) if contact.email else None
            resolved_company_id = contact.company_id or (
                domain_to_company_id.get(email_domain) if email_domain else None
            )

            result.append(
                {
                    "contact": contact,
                    "deal_count": row.deal_count or 0,
                    "source": self._map_source(integration_map.get(contact.integration_id)),
                    "company_id": resolved_company_id,
                    "company_domain": email_domain,
                }
            )

        return result, total_count

    async def get_contact_detail(self, contact_id: UUID) -> dict:
        """Full contact + deal involvement via contact participants."""
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        contact = res.scalars().first()

        if not contact:
            raise ContactReadServiceError("Contact not found", status_code=404)

        participant_contact_match = col(CRMDealParticipant.related_local_id) == contact_id
        if contact.email:
            participant_contact_match = or_(
                participant_contact_match,
                func.lower(func.trim(col(CRMDealParticipant.related_email)))
                == contact.email.strip().lower(),
            )

        deal_stmt = (
            select(CRMDeal, CRMDealParticipant)
            .join(
                CRMDealParticipant,
                (col(CRMDealParticipant.deal_id) == col(CRMDeal.deal_id))
                & participant_contact_match
                & (col(CRMDealParticipant.organisation_id) == self.organisation_id)
                & (col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value)
                & (col(CRMDealParticipant.is_deleted).is_(False))
                & (col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value),
            )
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.is_deleted).is_(False),
            )
        )
        deal_res = await self.org_session.execute(deal_stmt)
        deals = [
            {
                "deal": row.CRMDeal,
                "relationship_role": row.CRMDealParticipant.relationship_role,
                "participant_id": row.CRMDealParticipant.participant_id,
            }
            for row in deal_res.all()
        ]

        resolved_company_id = contact.company_id
        resolved_company_domain: str | None = None
        if resolved_company_id:
            company_domain_stmt = select(col(CRMCompany.domain)).where(
                col(CRMCompany.company_id) == resolved_company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            company_domain_res = await self.org_session.execute(company_domain_stmt)
            resolved_company_domain = company_domain_res.scalar()
        if not resolved_company_id:
            email_domain = self._extract_domain(contact.email) if contact.email else None
            if email_domain:
                company_match_stmt = select(
                    col(CRMCompany.company_id),
                    col(CRMCompany.domain),
                ).where(
                    col(CRMCompany.organisation_id) == self.organisation_id,
                    col(CRMCompany.is_deleted).is_(False),
                    col(CRMCompany.domain) == email_domain,
                )
                company_match_res = await self.org_session.execute(company_match_stmt)
                row = company_match_res.first()
                if row:
                    resolved_company_id = row[0]
                    resolved_company_domain = row[1]
                else:
                    resolved_company_domain = email_domain

        return {
            "contact": contact,
            "deals": deals,
            "company_id": resolved_company_id,
            "company_domain": resolved_company_domain,
            "source": await self.resolve_source_for_contact(contact.integration_id),
        }

    async def find_contact_by_email(self, email: str) -> CRMContact | None:
        """Return a non-deleted contact by exact case-insensitive email."""
        normalized_email = email.strip().lower()
        if not normalized_email:
            return None
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(col(CRMContact.email)) == normalized_email,
        )
        res = await self.org_session.execute(stmt)
        return res.scalars().first()
