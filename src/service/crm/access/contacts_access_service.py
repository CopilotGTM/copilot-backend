"""CRM service layer for accessing and managing contact data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

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

    @staticmethod
    def _map_source(integration_type: str | None) -> str:
        """Map an integration_type to the user-facing source label.

        Internal CRM contacts (manually created via the API) show as "manual".
        All other integration types pass through (hubspot, salesforce, google_calendar, etc.).
        """
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
        """Paginated contacts with deal_count, source, and company_id annotations.

        Source is derived from the integration_type of the owning integration.
        company_id is resolved by matching the contact's email domain against CRMCompany.domain.
        """
        if page < 1:
            raise ContactsAccessServiceError("Page must be >= 1", status_code=400)

        contact_participant_match = or_(
            col(CRMDealParticipant.related_local_id) == col(CRMContact.contact_id),
            func.lower(func.trim(col(CRMDealParticipant.related_email)))
            == func.lower(func.trim(col(CRMContact.email))),
        )

        # Subquery: count deals where this contact is an active approved contact participant.
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

        # Base query with filters and deal count annotation
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

        # Filter by company: explicit FK OR domain matching
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
                col(KnowledgeIntegration.integration_id),
                col(KnowledgeIntegration.integration_type),
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
            deal_count: int = row.deal_count or 0
            source = self._map_source(integration_map.get(contact.integration_id))
            email_domain = self._extract_domain(contact.email) if contact.email else None
            # Stored FK takes precedence over domain-resolved fallback
            resolved_company_id = contact.company_id or (
                domain_to_company_id.get(email_domain) if email_domain else None
            )

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
        """Full contact + deal involvement via contact participants."""
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        contact = res.scalars().first()

        if not contact:
            raise ContactsAccessServiceError("Contact not found", status_code=404)

        participant_contact_match = col(CRMDealParticipant.related_local_id) == contact_id
        if contact.email:
            participant_contact_match = or_(
                participant_contact_match,
                func.lower(func.trim(col(CRMDealParticipant.related_email)))
                == contact.email.strip().lower(),
            )

        # Fetch deals this contact is actively involved in (approved contact participants only)
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
        deal_rows = deal_res.all()

        deals = [
            {
                "deal": row.CRMDeal,
                "relationship_role": row.CRMDealParticipant.relationship_role,
                "stakeholder_id": row.CRMDealParticipant.participant_id,
            }
            for row in deal_rows
        ]

        # Resolve company_id and company_domain for the detail response
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
            # Fallback to domain matching
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

        source = await self.resolve_source_for_contact(contact.integration_id)

        return {
            "contact": contact,
            "deals": deals,
            "company_id": resolved_company_id,
            "company_domain": resolved_company_domain,
            "source": source,
        }

    async def create_contact(
        self,
        email: str,
        name: str,
        **kwargs: Any,
    ) -> CRMContact:
        """Create a contact, optionally linked to an account by explicit company_id.

        The frontend dropdown handles account suggestion before this call — if the
        user wanted to link to an account, they pass company_id explicitly. We do
        NOT auto-link based on email domain at create time; that's a runtime
        display fallback only.
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

        # Account linking is explicit-only — no silent auto-linking from email domain
        explicit_company_id: UUID | None = kwargs.pop("company_id", None)
        company_name: str | None = kwargs.pop("company_name", None)

        if explicit_company_id:
            co_stmt = select(CRMCompany).where(
                col(CRMCompany.company_id) == explicit_company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            co_res = await self.org_session.execute(co_stmt)
            company = co_res.scalars().first()
            if not company:
                raise ContactsAccessServiceError("Account not found", status_code=404)
            company_name = company_name or company.name

        contact_id = uuid4()
        contact = CRMContact(
            contact_id=contact_id,
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            email=email.strip().lower(),
            first_name=first_name,
            last_name=last_name,
            full_name=name.strip(),
            company_name=company_name,
            company_id=explicit_company_id,
            synced_at=datetime.now(UTC),
            is_deleted=False,
            properties={},
            metrics={},
            additional_emails=[],
            **kwargs,
        )

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.commit()
        await self.org_session.refresh(contact)
        return contact

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

    async def find_accounts_by_domain(self, domain: str) -> list[CRMCompany]:
        """Return active accounts with an exact normalized domain match."""
        from src.service.crm.access.accounts_access_service import AccountsAccessService

        service = AccountsAccessService(self.org_session, self.organisation_id)
        return await service.find_accounts_by_domain(domain)

    async def find_accounts_by_name(self, name: str) -> list[CRMCompany]:
        """Return active accounts with an exact case-insensitive name match."""
        from src.service.crm.access.accounts_access_service import AccountsAccessService

        service = AccountsAccessService(self.org_session, self.organisation_id)
        return await service.find_accounts_by_name(name)

    async def create_account(self, name: str, **kwargs: Any) -> CRMCompany:
        """Create an internal CRM account in the same org session as contact creation."""
        from src.service.crm.access.accounts_access_service import AccountsAccessService

        service = AccountsAccessService(self.org_session, self.organisation_id)
        return await service.create_account(name=name, **kwargs)

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

        # Email can only be set once — reject changes to an existing email
        new_email = updates.pop("email", None)
        if new_email is not None:
            if contact.email:
                raise ContactsAccessServiceError(
                    "Cannot change an existing email address",
                    status_code=400,
                )
            normalized_email = new_email.strip().lower()
            dup_stmt = select(col(CRMContact.contact_id)).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(col(CRMContact.email)) == normalized_email,
            )
            dup_res = await self.org_session.execute(dup_stmt)
            if dup_res.scalars().first():
                raise ContactsAccessServiceError(
                    f"A contact with email '{normalized_email}' already exists",
                    status_code=409,
                )
            contact.email = normalized_email

        # company_id is set-once: cannot be changed or unlinked once set
        if "company_id" in updates:
            new_company_id = updates.pop("company_id")
            if new_company_id is None:
                raise ContactsAccessServiceError(
                    "Cannot unlink a contact from its account",
                    status_code=400,
                )
            if contact.company_id:
                raise ContactsAccessServiceError(
                    "Cannot change the account linked to this contact",
                    status_code=400,
                )
            # Email precondition — checked after the email-set block above so a user can
            # set email and link an account in the same PATCH
            if not contact.email:
                raise ContactsAccessServiceError(
                    "Contact must have an email address before being linked to an account. "
                    "Add an email first.",
                    status_code=400,
                )
            co_stmt = select(CRMCompany).where(
                col(CRMCompany.company_id) == new_company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            co_res = await self.org_session.execute(co_stmt)
            company = co_res.scalars().first()
            if not company:
                raise ContactsAccessServiceError("Account not found", status_code=404)
            contact.company_id = new_company_id
            contact.company_name = company.name

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
        await self.org_session.commit()
        await self.org_session.refresh(contact)
        return contact

    async def add_contact_to_deal(
        self,
        contact_id: UUID,
        deal_id: UUID,
        relationship_role: str | None = None,
        actor_user_id: UUID | None = None,
    ) -> dict:
        """Add a contact to a deal as a participant.

        Delegates to DealParticipantService to keep participant creation, duplicate
        detection, and stakeholder compatibility logic centralized in one place.
        Import is deferred inside the method to avoid circular imports between
        the access and internal service layers.

        Args:
            contact_id: The contact to add.
            deal_id: The target deal.
            relationship_role: Optional role label (e.g. "decision_maker").
            actor_user_id: The user performing the action — used for audit trail.
        """
        # Verify contact exists, belongs to this org, and has an email
        contact_stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        contact_res = await self.org_session.execute(contact_stmt)
        contact = contact_res.scalars().first()
        if not contact:
            raise ContactsAccessServiceError("Contact not found", status_code=404)
        # Email is required for stakeholder matching and the unique constraint on (deal_id, email)
        if not contact.email:
            raise ContactsAccessServiceError(
                "Contact must have an email address before being added to a deal. "
                "Update the contact to add an email first.",
                status_code=400,
            )

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

        integration = KnowledgeIntegration.model_validate(
            {
                "organisation_id": self.organisation_id,
                # Sentinel user — this path is hit when no user context is available
                "created_by_user_id": UUID("00000000-0000-0000-0000-000000000001"),
                "created_by_admin": False,
                "integration_type": IntegrationType.INTERNAL_CRM,
                "integration_scope": IntegrationScope.ORGANIZATION,
                "name": "Copilot CRM",
                "credentials": {},
                "connection_metadata": {"source": "internal_crm", "internal": True},
                "is_active": True,
            }
        )
        self.org_session.add(integration)
        await self.org_session.flush()
        await self.org_session.refresh(integration)
        return integration
