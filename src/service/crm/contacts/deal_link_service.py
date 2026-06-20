"""Contact-to-deal participant linking service."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.contracts import (
    ContactDealLinkResult,
    ContactDealLinkServiceError,
)
from src.service.crm.internal.deal_participant_service import DealParticipantService


class ContactDealLinkService:
    """Tenant-scoped service for adding existing contacts to deals as participants."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Bind the service to one org-scoped session and immutable org boundary."""
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Close the owned org session when used as a FastAPI dependency."""
        await self.org_session.close()

    async def add_contact_to_deal(
        self,
        contact_id: UUID,
        deal_id: UUID,
        relationship_role: str | None = None,
        actor_user_id: UUID | None = None,
    ) -> ContactDealLinkResult:
        """Add a contact to a deal as a participant."""
        contact_stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        contact_res = await self.org_session.execute(contact_stmt)
        contact = contact_res.scalars().first()
        if not contact:
            raise ContactDealLinkServiceError("Contact not found", status_code=404)
        if not contact.email:
            raise ContactDealLinkServiceError(
                "Contact must have an email address before being added to a deal. "
                "Update the contact to add an email first.",
                status_code=400,
            )

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

        return ContactDealLinkResult(
            created=[
                {
                    "participant_id": participant.participant_id,
                    "participant_type": participant.participant_type,
                    "related_id": participant.related_local_id,
                    "relationship_role": participant.relationship_role,
                }
                for participant in created
            ],
            skipped=[
                {
                    "participant_type": s.participant_type,
                    "related_id": s.related_id,
                    "relationship_role": s.relationship_role,
                    "reason": s.reason,
                }
                for s in skipped
            ],
            contact_participant_changed=contact_changed,
        )
