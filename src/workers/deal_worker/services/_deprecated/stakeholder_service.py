"""DEPRECATED: Stakeholder discovery and sync service.

DEPRECATION NOTICE (January 2026):
----------------------------------
This module is DEPRECATED and moved to _deprecated folder.

Replaced by:
- DealMeetingMatcher now has inline stakeholder discovery
- src/workers/deal_worker/services/stakeholder_sync_service.py (CRM sync - Task 1)
- src/workers/deal_worker/services/stakeholder_lookup_service.py (lookup maps)

This file is no longer imported by any active code.
Can be deleted once confirmed safe.

Do NOT use this service for new development.
----------------------------------
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMParticipantType
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DealStakeholder,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class StakeholderService:
    """
    Service to discover and sync stakeholders for deals.

    Stakeholders are identified from two sources:
    1. CRM Participants (CONTACT type): Directly added as stakeholders
    2. Meeting Attendees: From deal-linked calendar events, anyone sharing
       the same domain as CRM contacts is added

    Selection Logic (NOT exclusion):
    - CRM contacts are always added (they are the known customer contacts)
    - From meetings, we ADD people who share the customer's domain
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        """
        Initialize the StakeholderService.

        Args:
            session: Async database session for the organisation
            organisation_id: UUID of the organisation
        """
        self.session = session
        self.organisation_id = organisation_id

    def _extract_email_domain(self, email: str) -> str | None:
        """Extract the domain part from an email address."""
        if not email or "@" not in email:
            return None
        return email.split("@")[1].lower()

    async def sync_stakeholders_from_deal(self, deal: CRMDeal) -> tuple[int, set[str]]:
        """
        Extract stakeholders from CRM participants and collect customer domains.

        Sources:
        1. Deal's primary contact
        2. Deal participants with participant_type == CONTACT

        Returns:
            Tuple of (processed_count, customer_domains)
            - customer_domains: Set of email domains from CRM contacts
        """
        processed_count = 0
        customer_domains: set[str] = set()

        # 1. Process Primary Contact
        if deal.primary_contact_email:
            await self._ensure_stakeholder(
                deal=deal,
                email=deal.primary_contact_email,
                name=deal.primary_contact_name,
                source=StakeholderSource.CRM,
                role="Primary Contact",
            )
            processed_count += 1
            domain = self._extract_email_domain(deal.primary_contact_email)
            if domain:
                customer_domains.add(domain)

        # 2. Process Contact Participants (not USER - those are internal team)
        if deal.participants:
            for participant in deal.participants:
                # Only CONTACT participants, not USER (owner/collaborators)
                if participant.participant_type != CRMParticipantType.CONTACT:
                    continue
                if not participant.related_email:
                    continue

                await self._ensure_stakeholder(
                    deal=deal,
                    email=participant.related_email,
                    name=participant.related_name,
                    source=StakeholderSource.CRM,
                    role=participant.relationship_role,
                )
                processed_count += 1
                domain = self._extract_email_domain(participant.related_email)
                if domain:
                    customer_domains.add(domain)

        logger.info(
            "Stakeholder sync from deal %s: processed=%d CRM contacts, customer_domains=%s",
            deal.deal_id,
            processed_count,
            customer_domains,
        )

        return processed_count, customer_domains

    async def sync_stakeholders_from_event(
        self,
        deal: CRMDeal,
        event: CalendarEvent,
        customer_domains: set[str],
    ) -> int:
        """
        Extract stakeholders from a calendar event linked to the deal.

        Only adds attendees who share the same domain as CRM contacts.
        This discovers additional customer stakeholders from meetings.

        Args:
            deal: The CRM deal
            event: Calendar event linked to the deal
            customer_domains: Set of domains from CRM contacts (customer domains)

        Returns:
            Number of stakeholders processed/updated.
        """
        if not event.attendees:
            logger.debug(
                "No attendees for event %s, skipping stakeholder sync",
                event.event_id,
            )
            return 0

        if not customer_domains:
            logger.debug(
                "No customer domains provided for deal %s, skipping event stakeholder sync",
                deal.deal_id,
            )
            return 0

        processed_count = 0
        skipped_count = 0

        for attendee in event.attendees:
            if not attendee.email:
                continue

            email = attendee.email.lower()
            domain = self._extract_email_domain(email)

            # Only add attendees who share the customer's domain
            if domain not in customer_domains:
                skipped_count += 1
                continue

            # Create or update stakeholder
            stakeholder = await self._upsert_stakeholder_from_attendee(
                deal=deal,
                email=email,
                display_name=attendee.display_name,
                event=event,
            )
            if stakeholder:
                processed_count += 1

        logger.debug(
            "Stakeholder sync from event %s for deal %s: added=%d, skipped=%d (not customer domain)",
            event.event_id,
            deal.deal_id,
            processed_count,
            skipped_count,
        )

        return processed_count

    async def _upsert_stakeholder_from_attendee(
        self,
        deal: CRMDeal,
        email: str,
        display_name: str | None,
        event: CalendarEvent,
    ) -> DealStakeholder | None:
        """Create or update a stakeholder from a calendar attendee."""
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal.deal_id,
            col(DealStakeholder.email) == email,
        )
        result = await self.session.execute(stmt)
        stakeholder = result.scalars().first()

        if not stakeholder:
            stakeholder = DealStakeholder(
                organisation_id=self.organisation_id,
                integration_id=deal.integration_id,
                deal_id=deal.deal_id,
                email=email,
                name=display_name,
                source=StakeholderSource.CALENDAR_DISCOVERY,
                stance=StakeholderStance.UNKNOWN,
                temperature=StakeholderTemperature.COLD,
                engagement_score=0.0,
            )
            # Try to enrich from CRM Contact
            await self._enrich_from_contact(stakeholder, email)
            self.session.add(stakeholder)

        # Update engagement metrics
        self._update_engagement_metrics(stakeholder, event)

        return stakeholder

    async def _ensure_stakeholder(
        self,
        deal: CRMDeal,
        email: str,
        name: str | None,
        source: StakeholderSource,
        role: str | None = None,
    ) -> DealStakeholder | None:
        """Create or update a stakeholder for the deal."""
        if not email:
            return None
        email = email.lower()

        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal.deal_id,
            col(DealStakeholder.email) == email,
        )
        result = await self.session.execute(stmt)
        stakeholder = result.scalars().first()

        if not stakeholder:
            stakeholder = DealStakeholder(
                organisation_id=self.organisation_id,
                integration_id=deal.integration_id,
                deal_id=deal.deal_id,
                email=email,
                name=name,
                source=source,
                stance=StakeholderStance.UNKNOWN,
                temperature=StakeholderTemperature.COLD,
                engagement_score=0.0,
                job_title=role,
            )
            await self._enrich_from_contact(stakeholder, email)
            self.session.add(stakeholder)

        return stakeholder

    async def _enrich_from_contact(self, stakeholder: DealStakeholder, email: str) -> None:
        """Enrich stakeholder with CRM contact data if available."""
        contact_stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.email) == email,
        )
        contact_result = await self.session.execute(contact_stmt)
        contact = contact_result.scalars().first()

        if contact:
            stakeholder.contact_id = contact.contact_id
            stakeholder.source = StakeholderSource.CRM
            if not stakeholder.name and contact.full_name:
                stakeholder.name = contact.full_name
            if contact.job_title:
                stakeholder.job_title = contact.job_title

    def _update_engagement_metrics(
        self, stakeholder: DealStakeholder, event: CalendarEvent
    ) -> None:
        """Update stakeholder engagement metrics based on meeting."""
        if not stakeholder.last_contact_at or event.start_at > stakeholder.last_contact_at:
            stakeholder.last_contact_at = event.start_at

        stakeholder.engagement_score += 1.0

        if stakeholder.engagement_score > 5:
            stakeholder.temperature = StakeholderTemperature.HOT
        elif stakeholder.engagement_score > 2:
            stakeholder.temperature = StakeholderTemperature.WARM
