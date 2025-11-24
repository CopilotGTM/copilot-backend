from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.services.stakeholder_service import StakeholderService

logger = get_logger(__name__)


class DealMeetingMatcher:
    """Service to match calendar events to CRM deals based on owner and contact participation."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        """Initialize the matcher with session and organisation context."""
        self.session = session
        self.organisation_id = organisation_id
        self.stakeholder_service = StakeholderService(session, organisation_id)
        self._contact_cache_by_id: dict[UUID, CRMContact] = {}
        self._contact_cache_by_external: dict[str, CRMContact] = {}

    def _normalize_email(self, email: str | None) -> str | None:
        """Normalize email string by stripping whitespace and converting to lowercase."""
        if not email:
            return None
        normalized = email.strip().lower()
        if not normalized or normalized == "null":
            return None
        return normalized

    def _get_participants(self, deal: CRMDeal) -> list[CRMDealParticipant]:
        """Retrieve the list of participants associated with a deal."""
        participants = cast(list[CRMDealParticipant] | None, getattr(deal, "participants", None))
        if participants is None:
            return []
        return participants

    def _get_participant_email(self, deal: CRMDeal, role: str) -> str | None:
        """Find the email of a user participant with a specific relationship role."""
        role = role.lower()
        for participant in self._get_participants(deal):
            if participant.participant_type != CRMParticipantType.USER:
                continue
            if not participant.relationship_role:
                continue
            if participant.relationship_role.lower() != role:
                continue
            normalized = self._normalize_email(participant.related_email)
            if normalized:
                return normalized
        return None

    def _add_contact_emails(self, contact: CRMContact | None, collector: set[str]) -> None:
        """Add primary and additional emails from a contact to the collector set."""
        if not contact:
            return
        primary = self._normalize_email(contact.email)
        if primary:
            collector.add(primary)
        for email in contact.additional_emails or []:
            normalized = self._normalize_email(email)
            if normalized:
                collector.add(normalized)

    async def _get_contact_by_id(self, contact_id: UUID) -> CRMContact | None:
        """Retrieve a contact by its local ID, using a local cache."""
        if contact_id in self._contact_cache_by_id:
            return self._contact_cache_by_id[contact_id]
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id) == contact_id,
        )
        result = await self.session.execute(stmt)
        contact = result.scalars().first()
        if contact:
            self._contact_cache_by_id[contact_id] = contact
            if contact.external_id:
                self._contact_cache_by_external[contact.external_id] = contact
        return contact

    async def _get_contact_by_external_id(self, external_id: str) -> CRMContact | None:
        """Retrieve a contact by its external ID, using a local cache."""
        cached = self._contact_cache_by_external.get(external_id)
        if cached:
            return cached
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.external_id) == external_id,
        )
        result = await self.session.execute(stmt)
        contact = result.scalars().first()
        if contact:
            self._contact_cache_by_external[external_id] = contact
            self._contact_cache_by_id[contact.contact_id] = contact
        return contact

    async def _collect_contact_emails(self, deal: CRMDeal) -> set[str]:
        """Collect all relevant contact emails (primary and participants) for a deal."""
        emails: set[str] = set()

        # 1. Add primary contact email directly from deal
        primary_email = self._normalize_email(deal.primary_contact_email)
        if primary_email:
            emails.add(primary_email)
        # 2. If primary email missing, lookup via external ID
        elif deal.primary_contact_external_id:
            contact = await self._get_contact_by_external_id(deal.primary_contact_external_id)
            self._add_contact_emails(contact, emails)

        # 3. Add emails from all contact-type participants
        for participant in self._get_participants(deal):
            if participant.participant_type != CRMParticipantType.CONTACT:
                continue

            # Try direct email on participant record
            normalized = self._normalize_email(participant.related_email)
            if normalized:
                emails.add(normalized)
                continue

            # Fallback to looking up the contact entity
            p_contact: CRMContact | None = None
            if participant.related_local_id:
                p_contact = await self._get_contact_by_id(participant.related_local_id)
            elif participant.related_external_id:
                p_contact = await self._get_contact_by_external_id(participant.related_external_id)
            self._add_contact_emails(p_contact, emails)

        return emails

    async def _resolve_owner_email(self, deal: CRMDeal) -> str | None:
        """Resolve deal owner email, prioritizing technical owner participant."""
        # Priority 1: Technical Owner (often the SE/SA on the deal)
        technical_owner = self._get_participant_email(deal, "technical_owner")
        if technical_owner:
            return technical_owner
        # Priority 2: Deal Owner (Sales Rep)
        return self._normalize_email(deal.owner_email)

    async def process_organisation(self) -> dict[str, int]:
        """
        Process all open deals for the organisation.
        """
        stats = {
            "deals_processed": 0,
            "meetings_linked": 0,
        }

        # Fetch all open deals
        deal_participants_rel = cast(Any, CRMDeal.participants)

        stmt = (
            select(CRMDeal)
            .options(selectinload(deal_participants_rel))
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                col(CRMDeal.is_deleted).is_(False),
            )
        )
        result = await self.session.execute(stmt)
        deals = result.scalars().all()

        logger.info(f"Processing {len(deals)} open deals for organisation {self.organisation_id}")

        for deal in deals:
            stats["deals_processed"] += 1
            linked_count = await self._find_and_link_meetings(deal)
            stats["meetings_linked"] += linked_count

        await self.session.commit()
        return stats

    async def process_deal(self, deal_id: UUID) -> dict[str, int]:
        """
        Process a single deal.
        """
        deal_participants_rel = cast(Any, CRMDeal.participants)
        stmt = (
            select(CRMDeal)
            .options(selectinload(deal_participants_rel))
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_id) == deal_id,
            )
        )
        result = await self.session.execute(stmt)
        deal = result.scalars().first()

        if not deal:
            logger.warning(f"Deal {deal_id} not found in org {self.organisation_id}")
            return {"deals_processed": 0, "meetings_linked": 0}

        linked_count = await self._find_and_link_meetings(deal)
        await self.session.commit()

        return {"deals_processed": 1, "meetings_linked": linked_count}

    async def _find_and_link_meetings(self, deal: CRMDeal) -> int:
        """
        Find matching meetings for a deal and create associations.
        """
        # 1. Identify the deal owner (technical or sales)
        owner_email = await self._resolve_owner_email(deal)
        if not owner_email:
            return 0

        # 2. Collect all known emails for deal contacts
        contact_emails = await self._collect_contact_emails(deal)
        contact_emails.discard(owner_email)  # Avoid self-matching if owner is also a contact

        if not contact_emails:
            return 0

        # 3. Build query to find relevant calendar events
        # We need events where:
        #   (Owner is Organiser OR Owner is an Attendee)
        #   AND
        #   (At least one Contact is an Attendee)

        attendees_relationship = cast(Any, CalendarEvent.attendees)
        owner_attendee = aliased(CalendarAttendee)
        contact_attendee = aliased(CalendarAttendee)

        owner_email_lower = owner_email
        contact_email_list = list(contact_emails)

        events_stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_relationship))
            # Join for checking if owner is an attendee
            .outerjoin(owner_attendee, col(owner_attendee.event_id) == col(CalendarEvent.event_id))
            # Join for checking if a contact is an attendee
            .join(contact_attendee, col(contact_attendee.event_id) == col(CalendarEvent.event_id))
            .where(col(CalendarEvent.organisation_id) == self.organisation_id)
            .where(
                or_(
                    # Condition A: Owner is the organiser
                    func.lower(CalendarEvent.organiser_email) == owner_email_lower,
                    # Condition B: Owner is an attendee
                    func.lower(owner_attendee.email) == owner_email_lower,
                )
            )
            # Condition C: One of the attendees matches a contact email
            .where(func.lower(contact_attendee.email).in_(contact_email_list))
            .distinct()
        )

        events_result = await self.session.execute(events_stmt)
        matching_events = events_result.scalars().all()

        logger.info(f"Found {len(matching_events)} matching events for deal {deal.deal_id}")

        linked_count = 0
        for event in matching_events:
            # Check if association already exists
            assoc_stmt = select(CalendarEventDealAssociation).where(
                col(CalendarEventDealAssociation.event_id) == event.event_id,
                col(CalendarEventDealAssociation.deal_id) == deal.deal_id,
            )
            assoc_result = await self.session.execute(assoc_stmt)
            existing_assoc = assoc_result.scalars().first()

            if not existing_assoc:
                # Create new association
                new_assoc = CalendarEventDealAssociation(
                    organisation_id=self.organisation_id,
                    event_id=event.event_id,
                    deal_id=deal.deal_id,
                    status=AssociationStatus.SUGGESTED,
                    match_score=1.0,  # Owner + contact co-attendance is high confidence
                    meeting_prep_status=GenerationStatus.NOT_STARTED,
                )
                self.session.add(new_assoc)
                linked_count += 1

                # Point 2.2 - Trigger Stakeholder Discovery
                await self.stakeholder_service.sync_stakeholders_from_event(deal, event)

        return linked_count


async def process_deal_meetings(session: AsyncSession, organisation_id: UUID) -> dict[str, int]:
    """
    Legacy wrapper for backward compatibility if needed, or for the main task entry point.
    """
    matcher = DealMeetingMatcher(session, organisation_id)
    return await matcher.process_organisation()
