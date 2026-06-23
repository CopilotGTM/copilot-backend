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
        """Paginated contacts with deal_count, source, company, and owner annotations."""
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
            domain
            for row in rows
            if row.CRMContact.email
            for domain in [self._extract_domain(row.CRMContact.email)]
            if domain
        }
        company_ids = {row.CRMContact.company_id for row in rows if row.CRMContact.company_id}
        company_by_id: dict[UUID, CRMCompany] = {}
        company_by_domain: dict[str, CRMCompany] = {}
        if company_ids or domains:
            co_stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
                or_(
                    col(CRMCompany.company_id).in_(list(company_ids)),
                    col(CRMCompany.domain).in_([d for d in domains if d]),
                ),
            )
            co_res = await self.org_session.execute(co_stmt)
            for company in co_res.scalars().all():
                company_by_id[company.company_id] = company
                if company.domain:
                    company_by_domain[company.domain.lower()] = company

        result: list[dict] = []
        for row in rows:
            contact: CRMContact = row.CRMContact
            email_domain = self._extract_domain(contact.email) if contact.email else None
            company = None
            if contact.company_id:
                company = company_by_id.get(contact.company_id)
            elif email_domain:
                company = company_by_domain.get(email_domain)

            result.append(
                {
                    "contact": contact,
                    "deal_count": row.deal_count or 0,
                    "source": self._map_source(integration_map.get(contact.integration_id)),
                    "company": self._contact_company_payload(
                        contact=contact,
                        company=company,
                        email_domain=email_domain,
                    ),
                    "owner": self._contact_owner_payload(contact),
                    "company_id": company.company_id if company else contact.company_id,
                    "company_domain": email_domain,
                }
            )

        return result, total_count

    @staticmethod
    def _contact_company_payload(
        *,
        contact: CRMContact,
        company: CRMCompany | None,
        email_domain: str | None,
    ) -> dict | None:
        """Build the nested company object used by list responses."""
        company_id = company.company_id if company else contact.company_id
        name = company.name if company else contact.company_name
        domain = company.domain if company else email_domain
        if not any((company_id, name, domain)):
            return None
        return {"id": company_id, "name": name, "domain": domain}

    @staticmethod
    def _contact_owner_payload(contact: CRMContact) -> dict | None:
        """Build the nested owner object used by list responses."""
        if not any((contact.owner_id, contact.owner_name, contact.owner_email)):
            return None
        return {
            "id": contact.owner_id,
            "name": contact.owner_name,
            "email": contact.owner_email,
        }

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
                "participant": row.CRMDealParticipant,
            }
            for row in deal_res.all()
        ]

        resolved_company_id = contact.company_id
        email_domain = self._extract_domain(contact.email) if contact.email else None
        company: CRMCompany | None = None
        if resolved_company_id:
            company_stmt = select(CRMCompany).where(
                col(CRMCompany.company_id) == resolved_company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            company_res = await self.org_session.execute(company_stmt)
            company = company_res.scalars().first()
        if not resolved_company_id and email_domain:
            company_match_stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
                col(CRMCompany.domain) == email_domain,
            )
            company_match_res = await self.org_session.execute(company_match_stmt)
            company = company_match_res.scalars().first()
            if company:
                resolved_company_id = company.company_id

        return {
            "contact": contact,
            "deals": deals,
            "company": self._contact_detail_company_payload(
                contact=contact,
                company=company,
                email_domain=email_domain,
            ),
            "owner": self._contact_owner_payload(contact),
            "source": await self.resolve_source_for_contact(contact.integration_id),
        }

    @staticmethod
    def _contact_detail_company_payload(
        *,
        contact: CRMContact,
        company: CRMCompany | None,
        email_domain: str | None,
    ) -> dict | None:
        """Build the account object used by the contact detail response."""
        company_id = company.company_id if company else contact.company_id
        name = company.name if company else contact.company_name
        domain = company.domain if company else email_domain
        if not any((company_id, name, domain)):
            return None
        return {
            "id": company_id,
            "name": name,
            "domain": domain,
            "website": company.website if company else None,
            "industry": company.industry if company else None,
            "linkedin_url": company.linkedin_url if company else None,
            "country": company.country if company else None,
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
