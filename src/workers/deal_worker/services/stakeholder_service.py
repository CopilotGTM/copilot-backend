from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class StakeholderService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def sync_stakeholders_from_event(self, deal: CRMDeal, event: CalendarEvent) -> int:
        """
        Extract stakeholders from a calendar event and link them to the deal.
        Returns the number of stakeholders processed/updated.
        """
        if not event.attendees:
            return 0

        processed_count = 0

        # We want to exclude the organiser if it's the deal owner (internal)
        # But sometimes the owner is also an attendee.
        # For now, we process everyone. We can filter by domain later if needed.

        for attendee in event.attendees:
            if not attendee.email:
                continue

            # Skip if attendee is the deal owner
            if deal.owner_email and attendee.email.lower() == deal.owner_email.lower():
                continue

            # Check if stakeholder exists
            stmt = select(DealStakeholder).where(
                col(DealStakeholder.deal_id) == deal.deal_id,
                col(DealStakeholder.email) == attendee.email,
            )
            result = await self.session.execute(stmt)
            stakeholder = result.scalars().first()

            if not stakeholder:
                # Create new stakeholder
                stakeholder = DealStakeholder(
                    organisation_id=self.organisation_id,
                    integration_id=deal.integration_id,
                    deal_id=deal.deal_id,
                    email=attendee.email,
                    name=attendee.display_name,
                    source=StakeholderSource.CALENDAR_DISCOVERY,
                    stance=StakeholderStance.UNKNOWN,
                    temperature=StakeholderTemperature.COLD,  # Start cold
                    engagement_score=0.0,
                )

                # Try to link to CRM Contact
                contact_stmt = select(CRMContact).where(
                    col(CRMContact.organisation_id) == self.organisation_id,
                    col(CRMContact.email) == attendee.email,
                )
                contact_result = await self.session.execute(contact_stmt)
                contact = contact_result.scalars().first()

                if contact:
                    stakeholder.contact_id = contact.contact_id
                    stakeholder.source = StakeholderSource.CRM  # Upgrade source if found in CRM
                    if not stakeholder.name and contact.full_name:
                        stakeholder.name = contact.full_name
                    if contact.job_title:
                        stakeholder.job_title = contact.job_title

                self.session.add(stakeholder)

            # Update metrics (Simple logic for now)
            # Update last_contact_at if this event is more recent
            if not stakeholder.last_contact_at or event.start_at > stakeholder.last_contact_at:
                stakeholder.last_contact_at = event.start_at

            # Increment engagement score (simple counter for now)
            stakeholder.engagement_score += 1.0

            # Update temperature based on score
            if stakeholder.engagement_score > 5:
                stakeholder.temperature = StakeholderTemperature.HOT
            elif stakeholder.engagement_score > 2:
                stakeholder.temperature = StakeholderTemperature.WARM

            processed_count += 1

        return processed_count
