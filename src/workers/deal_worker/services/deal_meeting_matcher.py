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
from src.service.crm.deal_timeline_sync_service import build_meeting_timeline_event
from src.workers.deal_worker.services.stakeholder_service import StakeholderService

logger = get_logger(__name__)


class DealMeetingMatcher:
    """Service to match calendar events to CRM deals based on owner and contact participation."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
    ):
        """
        Initialize the matcher with session and organisation context.

        Args:
            session: Async database session for the organisation
            organisation_id: UUID of the organisation
        """
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

    def _collect_team_emails(self, deal: CRMDeal) -> set[str]:
        """Collect all team member (USER participant) emails for a deal.

        This includes all users with roles like owner, collaborator, split_owner, etc.
        Any team member's participation in a meeting qualifies it for matching.

        Args:
            deal: The CRM deal to collect team emails for.

        Returns:
            Set of normalized email addresses for all team members.
        """
        emails: set[str] = set()

        for participant in self._get_participants(deal):
            if participant.participant_type != CRMParticipantType.USER:
                continue

            normalized = self._normalize_email(participant.related_email)
            if normalized:
                emails.add(normalized)

        # Fallback: include deal.owner_email if no USER participants found with emails
        if not emails:
            owner_email = self._normalize_email(deal.owner_email)
            if owner_email:
                emails.add(owner_email)

        return emails

    async def process_organisation(self) -> dict[str, int]:
        """
        Process all open deals for the organisation.

        Returns:
            Stats dictionary with deals_processed, meetings_linked, and cancelled_rejected counts.
        """
        stats = {
            "deals_processed": 0,
            "meetings_linked": 0,
            "cancelled_rejected": 0,
        }

        # STEP 0: Handle cancelled meetings FIRST (before matching new ones)
        # This ensures we clean up any previously-mapped meetings that are now cancelled
        cancelled_count = await self._handle_cancelled_associations()
        stats["cancelled_rejected"] = cancelled_count

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
            # Sync CRM contact stakeholders and collect customer domains
            _, customer_domains = await self.stakeholder_service.sync_stakeholders_from_deal(deal)

            linked_count = await self._find_and_link_meetings(deal, customer_domains)
            stats["meetings_linked"] += linked_count

        await self.session.commit()
        return stats

    async def process_deal(self, deal_id: UUID) -> dict[str, int]:
        """
        Process a single deal.

        Returns:
            Stats dictionary with deals_processed, meetings_linked, and cancelled_rejected counts.
        """
        # STEP 0: Handle cancelled meetings FIRST for this deal
        cancelled_count = await self._handle_cancelled_associations(deal_id=deal_id)

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
            return {"deals_processed": 0, "meetings_linked": 0, "cancelled_rejected": 0}

        # Sync CRM contact stakeholders and collect customer domains
        _, customer_domains = await self.stakeholder_service.sync_stakeholders_from_deal(deal)

        linked_count = await self._find_and_link_meetings(deal, customer_domains)

        await self.session.commit()

        return {
            "deals_processed": 1,
            "meetings_linked": linked_count,
            "cancelled_rejected": cancelled_count,
        }

    async def _find_and_link_meetings(self, deal: CRMDeal, customer_domains: set[str]) -> int:
        """
        Find matching meetings for a deal and create associations.

        Uses a two-pass approach:
        - Pass 1: Team member is ORGANIZER + at least one contact is attendee (score: 1.0)
        - Pass 2: Team member is ATTENDEE + at least one contact is attendee (score: 0.8)

        In both cases:
        - Event must NOT be cancelled
        - Only ONE contact needs to be present (not all)

        Args:
            deal: The CRM deal to match meetings for.
            customer_domains: Set of customer email domains (derived from CRM contacts).

        Returns:
            Number of new associations created.
        """
        # 1. Collect all team member emails
        team_emails = self._collect_team_emails(deal)
        if not team_emails:
            logger.debug(f"No team emails found for deal {deal.deal_id}")
            return 0

        # 2. Collect all known emails for deal contacts
        contact_emails = await self._collect_contact_emails(deal)
        contact_emails -= team_emails  # Remove team emails to avoid self-matching

        if not contact_emails:
            logger.debug(f"No contact emails found for deal {deal.deal_id}")
            return 0

        team_email_list = list(team_emails)
        contact_email_list = list(contact_emails)

        # Track which events we've already linked to avoid duplicates across passes
        linked_event_ids: set[UUID] = set()
        linked_count = 0

        # PASS 1: Team member is ORGANIZER (highest confidence - score 1.0)
        organizer_count = await self._match_events_as_organizer(
            deal, team_email_list, contact_email_list, linked_event_ids, customer_domains
        )
        linked_count += organizer_count

        # PASS 2: Team member is ATTENDEE only (lower confidence - score 0.8)
        attendee_count = await self._match_events_as_attendee(
            deal, team_email_list, contact_email_list, linked_event_ids, customer_domains
        )
        linked_count += attendee_count

        logger.info(
            f"Linked {linked_count} meetings for deal {deal.deal_id} "
            f"(organizer={organizer_count}, attendee={attendee_count}, "
            f"team_emails={len(team_emails)}, contact_emails={len(contact_emails)})"
        )

        return linked_count

    async def _match_events_as_organizer(
        self,
        deal: CRMDeal,
        team_email_list: list[str],
        contact_email_list: list[str],
        linked_event_ids: set[UUID],
        customer_domains: set[str],
    ) -> int:
        """
        Match events where a team member is the ORGANIZER.

        This is the highest confidence match (score: 1.0) because:
        - Team member organized the meeting
        - At least one customer contact is attending

        Args:
            deal: The CRM deal to match.
            team_email_list: List of team member emails.
            contact_email_list: List of customer contact emails.
            linked_event_ids: Set of already-linked event IDs (will be updated).
            customer_domains: Set of customer email domains for stakeholder discovery.

        Returns:
            Number of new associations created.
        """
        attendees_relationship = cast(Any, CalendarEvent.attendees)
        contact_attendee = aliased(CalendarAttendee)

        # Query: Team member is ORGANIZER + contact is attendee + not cancelled
        events_stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_relationship))
            .join(contact_attendee, col(contact_attendee.event_id) == col(CalendarEvent.event_id))
            .where(col(CalendarEvent.organisation_id) == self.organisation_id)
            # Filter out cancelled events
            .where(
                or_(
                    col(CalendarEvent.status).is_(None),
                    col(CalendarEvent.status) != "cancelled",
                )
            )
            # Team member is the ORGANIZER
            .where(func.lower(CalendarEvent.organiser_email).in_(team_email_list))
            # At least one contact is an attendee
            .where(func.lower(contact_attendee.email).in_(contact_email_list))
            .distinct()
        )

        events_result = await self.session.execute(events_stmt)
        matching_events = list(events_result.scalars().all())

        return await self._create_associations(
            deal,
            matching_events,
            match_score=1.0,
            linked_event_ids=linked_event_ids,
            customer_domains=customer_domains,
        )

    async def _match_events_as_attendee(
        self,
        deal: CRMDeal,
        team_email_list: list[str],
        contact_email_list: list[str],
        linked_event_ids: set[UUID],
        customer_domains: set[str],
    ) -> int:
        """
        Match events where a team member is an ATTENDEE (but not organizer).

        This is a lower confidence match (score: 0.8) because:
        - Team member is attending but didn't organize
        - At least one customer contact is also attending

        Args:
            deal: The CRM deal to match.
            team_email_list: List of team member emails.
            contact_email_list: List of customer contact emails.
            linked_event_ids: Set of already-linked event IDs (will be updated).
            customer_domains: Set of customer email domains for stakeholder discovery.

        Returns:
            Number of new associations created.
        """
        attendees_relationship = cast(Any, CalendarEvent.attendees)
        team_attendee = aliased(CalendarAttendee)
        contact_attendee = aliased(CalendarAttendee)

        # Query: Team member is ATTENDEE + contact is attendee + not cancelled
        # Exclude events where team member is already the organizer (handled in pass 1)
        events_stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_relationship))
            .join(team_attendee, col(team_attendee.event_id) == col(CalendarEvent.event_id))
            .join(contact_attendee, col(contact_attendee.event_id) == col(CalendarEvent.event_id))
            .where(col(CalendarEvent.organisation_id) == self.organisation_id)
            # Filter out cancelled events
            .where(
                or_(
                    col(CalendarEvent.status).is_(None),
                    col(CalendarEvent.status) != "cancelled",
                )
            )
            # Team member is NOT the organizer (those are handled in pass 1)
            .where(
                or_(
                    col(CalendarEvent.organiser_email).is_(None),
                    ~func.lower(CalendarEvent.organiser_email).in_(team_email_list),
                )
            )
            # Team member is an attendee
            .where(func.lower(team_attendee.email).in_(team_email_list))
            # At least one contact is an attendee
            .where(func.lower(contact_attendee.email).in_(contact_email_list))
            .distinct()
        )

        events_result = await self.session.execute(events_stmt)
        matching_events = list(events_result.scalars().all())

        return await self._create_associations(
            deal,
            matching_events,
            match_score=0.8,
            linked_event_ids=linked_event_ids,
            customer_domains=customer_domains,
        )

    async def _create_associations(
        self,
        deal: CRMDeal,
        events: list[CalendarEvent],
        match_score: float,
        linked_event_ids: set[UUID],
        customer_domains: set[str],
    ) -> int:
        """
        Create associations for matched events.

        Args:
            deal: The CRM deal.
            events: List of matching calendar events.
            match_score: Confidence score for the match (1.0 = organizer, 0.8 = attendee).
            linked_event_ids: Set of already-linked event IDs (will be updated).
            customer_domains: Set of customer email domains for stakeholder discovery.

        Returns:
            Number of new associations created.
        """
        linked_count = 0

        for event in events:
            # Skip if already linked in a previous pass
            if event.event_id in linked_event_ids:
                continue

            # Check if association already exists in database
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
                    match_score=match_score,
                    meeting_prep_status=GenerationStatus.NOT_STARTED,
                )
                self.session.add(new_assoc)

                # Create timeline event inline (same transaction)
                timeline_event = build_meeting_timeline_event(
                    organisation_id=self.organisation_id,
                    deal_id=deal.deal_id,
                    association=new_assoc,
                    event=event,
                )
                self.session.add(timeline_event)

                linked_count += 1
                linked_event_ids.add(event.event_id)

                # Discover stakeholders from event attendees (only customer domain)
                await self.stakeholder_service.sync_stakeholders_from_event(
                    deal, event, customer_domains
                )

        return linked_count

    async def _handle_cancelled_associations(self, deal_id: UUID | None = None) -> int:
        """
        Update associations to REJECTED status when their events are cancelled.

        This handles the case where a meeting was previously matched but the
        underlying calendar event was later cancelled.

        Args:
            deal_id: Optional deal ID to scope the check. If None, checks all deals in org.

        Returns:
            Number of associations updated to REJECTED.
        """
        # Find associations linked to cancelled events that aren't already rejected
        stmt = (
            select(CalendarEventDealAssociation)
            .join(
                CalendarEvent,
                col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
            )
            .where(
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                col(CalendarEventDealAssociation.status) != AssociationStatus.REJECTED,
                col(CalendarEvent.status) == "cancelled",
            )
        )

        # Optionally scope to a specific deal
        if deal_id is not None:
            stmt = stmt.where(col(CalendarEventDealAssociation.deal_id) == deal_id)

        result = await self.session.execute(stmt)
        associations = result.scalars().all()

        updated_count = 0
        for assoc in associations:
            assoc.status = AssociationStatus.REJECTED
            updated_count += 1
            logger.info(
                f"Rejected cancelled event association: event_id={assoc.event_id}, "
                f"deal_id={assoc.deal_id}"
            )

        if updated_count > 0:
            logger.info(
                f"Rejected {updated_count} associations for cancelled events "
                f"(org={self.organisation_id}, deal={deal_id})"
            )

        return updated_count


async def process_deal_meetings(
    session: AsyncSession,
    organisation_id: UUID,
) -> dict[str, int]:
    """
    Process deal meetings for an organisation.

    Args:
        session: Async database session for the organisation
        organisation_id: UUID of the organisation

    Returns:
        Dictionary with processing statistics.
    """
    matcher = DealMeetingMatcher(session, organisation_id)
    return await matcher.process_organisation()
