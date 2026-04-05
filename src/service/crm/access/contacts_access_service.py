"""CRM service layer for accessing and managing contact data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.logger import get_logger

logger = get_logger("contacts_access_service")


class ContactsAccessServiceError(Exception):
    """Custom exception for contacts access service errors."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class ContactsAccessService:
    """Service for accessing and managing CRM contacts with filtering, pagination, and CRUD.

    All queries are scoped by organisation_id to enforce multi-tenant data isolation.
    Contacts are linked to accounts via email domain matching — no explicit FK exists
    between crm_contacts and crm_companies, so the domain is the join key.
    """

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Initialize the service with an org-scoped session.

        Args:
            org_session: Async database session scoped to the organisation.
            organisation_id: Immutable security boundary — all queries filter by this.
        """
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Clean up any resources."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("ContactsAccessService close exception", exc_info=True)

    @staticmethod
    def _extract_domain(email: str) -> str | None:
        """Extract lowered domain from email. Returns None if invalid."""
        if not email or "@" not in email:
            return None
        parts = email.rsplit("@", 1)
        domain = parts[1].strip().lower()
        return domain if domain else None

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str | None]:
        """Split 'First Last' into (first, last). Last may be None for single names."""
        parts = full_name.strip().split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else None
        return first, last

    async def list_contacts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        company_id: UUID | None = None,
    ) -> tuple[list[dict], int]:
        """Paginated contacts with deal_count, source, and company_id annotations.

        Source is derived from the integration_type of the owning integration.
        company_id is resolved by matching the contact's email domain against CRMCompany.domain.
        """
        if page < 1:
            raise ContactsAccessServiceError("Page must be >= 1", status_code=400)

        # Subquery: count deals where this contact appears as a stakeholder
        deal_count_sub = (
            select(func.count(col(DealStakeholder.stakeholder_id)))
            .where(
                col(DealStakeholder.contact_id) == col(CRMContact.contact_id),
                col(DealStakeholder.organisation_id) == self.organisation_id,
                col(DealStakeholder.is_deleted).is_(False),
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

        # Filter by company via domain matching
        if company_id is not None:
            company_stmt = select(CRMCompany.domain).where(
                col(CRMCompany.company_id) == company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            company_res = await self.org_session.execute(company_stmt)
            company_domain = company_res.scalar()
            if company_domain:
                base = base.where(
                    func.split_part(col(CRMContact.email), "@", 2).ilike(company_domain)
                )

        # Count is derived from the fully-filtered base so company_id filter is respected
        count_stmt = select(func.count()).select_from(base.subquery())
        count_res = await self.org_session.execute(count_stmt)
        total_count = count_res.scalar() or 0

        stmt = base

        offset = (page - 1) * limit
        stmt = (
            stmt.order_by(
                col(CRMContact.created_at).desc(),
                col(CRMContact.contact_id).desc(),
            )
            .offset(offset)
            .limit(limit)
        )

        rows = (await self.org_session.execute(stmt)).all()

        # Batch-resolve integration types for source annotation
        integration_ids = {row.CRMContact.integration_id for row in rows}
        integration_map: dict[UUID, str] = {}
        if integration_ids:
            int_stmt = select(
                KnowledgeIntegration.integration_id,
                KnowledgeIntegration.integration_type,
            ).where(
                col(KnowledgeIntegration.integration_id).in_(list(integration_ids)),
            )
            int_res = await self.org_session.execute(int_stmt)
            for int_id, int_type in int_res.all():
                integration_map[int_id] = int_type

        # Batch-resolve company_id by domain
        domains = {
            self._extract_domain(row.CRMContact.email) for row in rows if row.CRMContact.email
        }
        domain_to_company_id: dict[str, UUID] = {}
        if domains:
            co_stmt = select(CRMCompany.domain, CRMCompany.company_id).where(
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
            deal_count: int = row.deal_count or 0
            source = integration_map.get(contact.integration_id, IntegrationType.INTERNAL_CRM.value)
            email_domain = self._extract_domain(contact.email) if contact.email else None
            resolved_company_id = domain_to_company_id.get(email_domain) if email_domain else None

            result.append(
                {
                    "contact": contact,
                    "deal_count": deal_count,
                    "source": source,
                    "company_id": resolved_company_id,
                    "company_domain": email_domain,
                }
            )

        return result, total_count

    async def get_contact_detail(self, contact_id: UUID) -> dict:
        """Full contact + deal involvement via DealStakeholder JOIN CRMDeal."""
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        contact = res.scalars().first()

        if not contact:
            raise ContactsAccessServiceError("Contact not found", status_code=404)

        # Fetch deals this contact is involved in via DealStakeholder
        deal_stmt = (
            select(CRMDeal, DealStakeholder)
            .join(
                DealStakeholder,
                (col(DealStakeholder.deal_id) == col(CRMDeal.deal_id))
                & (col(DealStakeholder.contact_id) == contact_id)
                & (col(DealStakeholder.organisation_id) == self.organisation_id)
                & (col(DealStakeholder.is_deleted).is_(False)),
            )
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.is_deleted).is_(False),
            )
        )
        deal_res = await self.org_session.execute(deal_stmt)
        deal_rows = deal_res.all()

        deals = [
            {
                "deal": row.CRMDeal,
                "relationship_role": row.DealStakeholder.source_reference_id,
                "stakeholder_id": row.DealStakeholder.stakeholder_id,
            }
            for row in deal_rows
        ]

        return {
            "contact": contact,
            "deals": deals,
        }

    async def create_contact(
        self,
        email: str,
        name: str,
        **kwargs: Any,
    ) -> CRMContact:
        """Create a contact with auto account resolution from email domain.

        Checks for duplicate email (case-insensitive). Gets or creates the internal
        CRM integration. Auto-resolves company_name from email domain if not provided.
        """
        # Duplicate check — email uniqueness is a business rule for internally-created contacts
        dup_stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(col(CRMContact.email)) == email.strip().lower(),
        )
        dup_res = await self.org_session.execute(dup_stmt)
        if dup_res.scalars().first():
            raise ContactsAccessServiceError(
                f"A contact with email '{email}' already exists",
                status_code=409,
            )

        integration = await self._get_or_create_internal_integration()

        first_name, last_name = self._split_name(name)

        # Auto-resolve company_name from domain if not explicitly provided
        company_name: str | None = kwargs.pop("company_name", None)
        if not company_name:
            domain = self._extract_domain(email)
            if domain:
                co_stmt = select(CRMCompany.name).where(
                    col(CRMCompany.organisation_id) == self.organisation_id,
                    col(CRMCompany.is_deleted).is_(False),
                    col(CRMCompany.domain) == domain,
                )
                co_res = await self.org_session.execute(co_stmt)
                company_name = co_res.scalar()

        contact_id = uuid4()
        contact = CRMContact(
            contact_id=contact_id,
            # external_id mirrors the internal UUID for internally-created contacts
            external_id=str(contact_id),
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            email=email.strip().lower(),
            first_name=first_name,
            last_name=last_name,
            full_name=name.strip(),
            company_name=company_name,
            synced_at=datetime.now(UTC),
            is_deleted=False,
            properties={},
            metrics={},
            additional_emails=[],
            **kwargs,
        )

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        return contact

    async def update_contact(self, contact_id: UUID, updates: dict) -> CRMContact:
        """Update allowed fields on a contact. linkedin_url is stored via properties dict."""
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        contact = res.scalars().first()

        if not contact:
            raise ContactsAccessServiceError("Contact not found", status_code=404)

        # linkedin_url lives in the properties JSONB blob, not a top-level column
        linkedin_url = updates.pop("linkedin_url", None)
        if linkedin_url is not None:
            props = dict(contact.properties or {})
            props["linkedin_url"] = linkedin_url
            contact.properties = props

        for field, value in updates.items():
            if hasattr(contact, field):
                setattr(contact, field, value)

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        return contact

    async def add_contact_to_deal(
        self,
        contact_id: UUID,
        deal_id: UUID,
        relationship_role: str | None = None,
        actor_user_id: UUID | None = None,
    ) -> dict:
        """Add a contact to a deal as a participant (CRMDealParticipant + DealStakeholder).

        Delegates to DealParticipantService to keep participant creation, duplicate
        detection, and stakeholder sync logic centralized in one place.
        Import is deferred inside the method to avoid circular imports between
        the access and internal service layers.

        Args:
            contact_id: The contact to add.
            deal_id: The target deal.
            relationship_role: Optional role label (e.g. "decision_maker").
            actor_user_id: The user performing the action — used for audit trail.
        """
        # Verify contact belongs to this org before delegating
        contact_stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        contact_res = await self.org_session.execute(contact_stmt)
        if not contact_res.scalars().first():
            raise ContactsAccessServiceError("Contact not found", status_code=404)

        # Deferred import to avoid circular dependency between access and internal services
        from src.service.crm.internal.deal_participant_service import DealParticipantService

        participant_service = DealParticipantService(
            session=self.org_session,
            organisation_id=self.organisation_id,
            actor_user_id=actor_user_id or uuid4(),
        )

        (
            created,
            skipped,
            contact_changed,
            _integration_id,
        ) = await participant_service.add_participants(
            deal_id,
            [
                {
                    "participant_type": "contact",
                    "related_id": contact_id,
                    "relationship_role": relationship_role,
                }
            ],
        )

        return {
            "created": created,
            "skipped": [
                {
                    "participant_type": s.participant_type,
                    "related_id": s.related_id,
                    "relationship_role": s.relationship_role,
                    "reason": s.reason,
                }
                for s in skipped
            ],
            "contact_participant_changed": contact_changed,
        }

    async def _get_or_create_internal_integration(self) -> KnowledgeIntegration:
        """Return the internal CRM integration, creating it if absent."""
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            col(KnowledgeIntegration.integration_type) == IntegrationType.INTERNAL_CRM.value,
        )
        res = await self.org_session.execute(stmt)
        integration = res.scalars().first()
        if integration:
            return integration

        # Create the internal integration on first use — idempotent with InternalCRMSetupService
        integration = KnowledgeIntegration(
            organisation_id=self.organisation_id,
            # Sentinel user — this path is hit when no user context is available
            created_by_user_id=UUID("00000000-0000-0000-0000-000000000001"),
            created_by_admin=False,
            integration_type=IntegrationType.INTERNAL_CRM,
            integration_scope=IntegrationScope.ORGANIZATION,
            name="Copilot CRM",
            credentials={},
            connection_metadata={"source": "internal_crm", "internal": True},
            is_active=True,
        )
        self.org_session.add(integration)
        await self.org_session.flush()
        await self.org_session.refresh(integration)
        return integration
