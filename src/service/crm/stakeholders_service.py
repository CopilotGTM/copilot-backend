"""CRM service layer for managing deal stakeholders."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_models import (
    AddContactsAsStakeholdersResponse,
    AvailableContactItem,
    SkippedContact,
    StakeholderDetail,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    StakeholderSource,
)
from src.infra.utils.logger import get_logger

logger = get_logger("stakeholders_service")


class StakeholdersServiceError(Exception):
    """Custom exception for stakeholders service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class StakeholdersService:
    """Service for managing CRM deal stakeholders."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def list_stakeholders_by_deal(self, deal_id: UUID) -> list[DealStakeholder]:
        """Get all stakeholders associated with a specific deal."""
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_available_contacts(
        self, deal_id: UUID, query: str | None = None, limit: int = 50
    ) -> list[AvailableContactItem]:
        """List contacts that are not yet stakeholders for the deal.

        Only returns contacts with valid email addresses, as email is required
        for stakeholder identification.
        """
        # Subquery for existing stakeholders
        subq = select(col(DealStakeholder.contact_id)).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).not_in(subq),
            col(CRMContact.email).isnot(None),  # Only contacts with email
            col(CRMContact.email) != "",  # Exclude empty email strings
        )

        if query:
            search = f"%{query}%"
            stmt = stmt.where(
                (col(CRMContact.first_name).ilike(search))
                | (col(CRMContact.last_name).ilike(search))
                | (col(CRMContact.full_name).ilike(search))
                | (col(CRMContact.email).ilike(search))
                | (col(CRMContact.company_name).ilike(search))
            )

        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        contacts = list(result.scalars().all())

        # Convert to AvailableContactItem with relevant information
        return [
            AvailableContactItem(
                contact_id=c.contact_id,
                external_id=c.external_id,
                email=c.email,  # type: ignore - filtered above
                first_name=c.first_name,
                last_name=c.last_name,
                full_name=c.full_name,
                job_title=c.job_title,
                company_name=c.company_name,
                phone=c.phone,
                lifecycle_stage=c.lifecycle_stage,
                last_activity_at=c.last_activity_at,
            )
            for c in contacts
        ]

    async def add_contacts_as_stakeholders(
        self, deal_id: UUID, contact_ids: list[UUID]
    ) -> AddContactsAsStakeholdersResponse:
        """Add selected contacts as stakeholders to a deal.

        Only contacts with valid email addresses are added. Contacts without
        emails or those that already exist as stakeholders are skipped.
        """
        added: list[StakeholderDetail] = []
        skipped: list[SkippedContact] = []

        # Fetch all requested contacts
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).in_(contact_ids),
        )
        result = await self.session.execute(stmt)
        contacts_map = {c.contact_id: c for c in result.scalars().all()}

        # Get existing stakeholder emails for this deal
        existing_stmt = select(DealStakeholder.email, DealStakeholder.contact_id).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        existing_result = await self.session.execute(existing_stmt)
        existing_rows = existing_result.all()
        existing_emails = {row.email.lower() for row in existing_rows if row.email}
        existing_contact_ids = {row.contact_id for row in existing_rows if row.contact_id}

        for contact_id in contact_ids:
            contact = contacts_map.get(contact_id)

            # Contact not found
            if not contact:
                skipped.append(SkippedContact(contact_id=contact_id, reason="Contact not found"))
                continue

            # No email
            if not contact.email or not contact.email.strip():
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Contact has no email address",
                    )
                )
                continue

            # Already exists by email
            if contact.email.lower() in existing_emails:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Stakeholder with this email already exists",
                    )
                )
                continue

            # Already exists by contact_id
            if contact_id in existing_contact_ids:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Contact is already a stakeholder",
                    )
                )
                continue

            # Build name from contact
            name = (
                contact.full_name
                or " ".join(filter(None, [contact.first_name, contact.last_name]))
                or None
            )

            # Create the stakeholder
            new_stakeholder = DealStakeholder(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                contact_id=contact.contact_id,
                email=contact.email,
                name=name,
                job_title=contact.job_title,
                source=StakeholderSource.CRM,
            )
            self.session.add(new_stakeholder)

            # Track for response - add email to existing set
            existing_emails.add(contact.email.lower())
            existing_contact_ids.add(contact_id)

        # Commit all new stakeholders
        await self.session.commit()

        # Refresh added stakeholders for response
        added_stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.contact_id).in_(
                [cid for cid in contact_ids if cid not in {s.contact_id for s in skipped}]
            ),
        )
        added_result = await self.session.execute(added_stmt)
        added_stakeholders = list(added_result.scalars().all())

        # Convert to StakeholderDetail
        added = [StakeholderDetail.model_validate(s) for s in added_stakeholders]

        return AddContactsAsStakeholdersResponse(
            added=added,
            skipped=skipped,
            total_requested=len(contact_ids),
            total_added=len(added),
            total_skipped=len(skipped),
        )

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("StakeholdersService close exception", exc_info=True)
