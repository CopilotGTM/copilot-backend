from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
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
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.timeline.deal_timeline_sync_service import build_meeting_timeline_event
from src.service.crm.timeline.timeline_event_source_resolver import resolve_meeting_event_source

logger = get_logger(__name__)

# Max auto-approved discovered stakeholders per deal from calendar events
MAX_CALENDAR_DISCOVERY_AUTO_APPROVED = 5


class DealMeetingMatcher:
    """Service to match calendar events to CRM deals based on owner and contact participation."""

    def __init__(
        self,
        organisation_id: UUID,
        session: AsyncSession | None = None,
    ):
        """
        Initialize the matcher with organisation context.

        Args:
            organisation_id: UUID of the organisation
            session: Optional async database session. If not provided,
                     process_organisation() will acquire fresh sessions.
                     Required for process_deal().
        """
        self.organisation_id = organisation_id
        self._session = session  # Optional - used by process_deal()
        self._contact_cache_by_id: dict[UUID, CRMContact] = {}
        self._contact_cache_by_external: dict[str, CRMContact] = {}
        self._internal_domains: set[str] | None = None  # Lazy-loaded cache

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

    async def _get_contact_by_id(
        self, session: AsyncSession, contact_id: UUID
    ) -> CRMContact | None:
        """Retrieve a contact by its local ID, using a local cache."""
        if contact_id in self._contact_cache_by_id:
            return self._contact_cache_by_id[contact_id]
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id) == contact_id,
        )
        result = await session.execute(stmt)
        contact = result.scalars().first()
        if contact:
            self._contact_cache_by_id[contact_id] = contact
            if contact.external_id:
                self._contact_cache_by_external[contact.external_id] = contact
        return contact

    async def _get_contact_by_external_id(
        self, session: AsyncSession, external_id: str
    ) -> CRMContact | None:
        """Retrieve a contact by its external ID, using a local cache."""
        cached = self._contact_cache_by_external.get(external_id)
        if cached:
            return cached
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.external_id) == external_id,
        )
        result = await session.execute(stmt)
        contact = result.scalars().first()
        if contact:
            self._contact_cache_by_external[external_id] = contact
            self._contact_cache_by_id[contact.contact_id] = contact
        return contact

    async def _collect_contact_emails(self, session: AsyncSession, deal: CRMDeal) -> set[str]:
        """Collect all approved stakeholder emails for a deal.

        Uses DealStakeholder table which contains:
        - CRM-synced contacts (source=CRM)
        - Discovered contacts from calendar (source=CALENDAR_DISCOVERY)

        Only returns APPROVED stakeholders (not PENDING or REJECTED).
        """
        emails: set[str] = set()

        # Get all approved stakeholders from DealStakeholder table
        stmt = select(col(DealStakeholder.email)).where(
            col(DealStakeholder.deal_id) == deal.deal_id,
            col(DealStakeholder.is_deleted).is_(False),
            col(DealStakeholder.approval_status) == ApprovalStatus.APPROVED,
        )
        result = await session.execute(stmt)
        stakeholder_emails = result.scalars().all()

        for email in stakeholder_emails:
            normalized = self._normalize_email(email)
            if normalized:
                emails.add(normalized)

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

    async def _collect_internal_domains(self, session: AsyncSession) -> set[str]:
        """Collect the organisation's internal email domains.

        Uses Organisation.domain (admin-configured). If empty, derives from
        all USER participant emails across all deals in the org.

        Cached after first call to avoid repeated DB lookups.
        """
        if self._internal_domains is not None:
            return self._internal_domains

        domains: set[str] = set()

        # Tier 1: Organisation.domain (authoritative)
        # Organisation lives in the public schema — requires a master session.
        async for master_session in acquire_master_async_session():
            org_stmt = select(col(Organisation.domain)).where(
                col(Organisation.organisation_id) == self.organisation_id,
            )
            org_result = await master_session.execute(org_stmt)
            domains_raw = org_result.scalar_one_or_none()
            if domains_raw and isinstance(domains_raw, list):
                domains = {d.strip().lower() for d in domains_raw if d and d.strip()}

        # Tier 2: Derive from all USER participant emails in the org
        if not domains:
            participant_stmt = select(func.distinct(col(CRMDealParticipant.related_email))).where(
                col(CRMDealParticipant.participant_type) == CRMParticipantType.USER,
            )
            participant_result = await session.execute(participant_stmt)
            for (email,) in participant_result.all():
                if email and "@" in email:
                    domain = email.strip().lower().split("@")[-1]
                    domains.add(domain)

        self._internal_domains = domains
        return domains

    def _is_internal_email(self, email: str, internal_domains: set[str]) -> bool:
        """Check if an email belongs to an internal domain."""
        if not email or "@" not in email:
            return False
        domain = email.split("@")[-1]
        return domain in internal_domains

    def _extract_domain(self, email: str) -> str | None:
        """Extract domain from email address."""
        if not email or "@" not in email:
            return None
        return email.split("@")[1].lower()

    def _resolve_completed_contact_time(self, event: CalendarEvent) -> datetime | None:
        """Return event contact time only for completed meetings (never upcoming)."""
        candidate = event.end_at or event.start_at
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=UTC)
        if candidate > datetime.now(UTC):
            return None
        return candidate

    def _collect_event_participant_emails(self, event: CalendarEvent) -> set[str]:
        """Collect normalized participant emails from organizer + attendees."""
        emails: set[str] = set()
        organizer_email = self._normalize_email(event.organiser_email)
        if organizer_email:
            emails.add(organizer_email)

        for attendee in event.attendees or []:
            normalized = self._normalize_email(attendee.email)
            if normalized:
                emails.add(normalized)

        return emails

    def _collect_customer_domains(self, deal: CRMDeal) -> set[str]:
        """Collect customer domains from deal's CONTACT participants.

        These domains are used to identify additional stakeholders from
        calendar event attendees who belong to the same company.

        Args:
            deal: The CRM deal.

        Returns:
            Set of customer email domains.
        """
        domains: set[str] = set()

        # From primary contact
        if deal.primary_contact_email:
            domain = self._extract_domain(deal.primary_contact_email)
            if domain:
                domains.add(domain)

        # From CONTACT participants
        for participant in self._get_participants(deal):
            if participant.participant_type != CRMParticipantType.CONTACT:
                continue
            if participant.related_email:
                domain = self._extract_domain(participant.related_email)
                if domain:
                    domains.add(domain)

        return domains

    async def _discover_stakeholders_from_event(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        event: CalendarEvent,
        customer_domains: set[str],
        team_emails: set[str],
    ) -> int:
        """Discover new stakeholders from calendar event attendees.

        Only adds attendees who:
        - Have email domain matching customer domains
        - Are not already team members
        - Don't already exist as stakeholders for this deal

        Applies 5-limit: first 5 discovered stakeholders are auto-APPROVED,
        beyond that they are PENDING (require user approval).

        Args:
            session: Database session to use.
            deal: The CRM deal.
            event: Calendar event with attendees.
            customer_domains: Set of customer email domains.
            team_emails: Set of team member emails to exclude.

        Returns:
            Number of stakeholders discovered.
        """
        if not event.attendees or not customer_domains:
            return 0

        discovered = 0
        contact_time = self._resolve_completed_contact_time(event)

        for attendee in event.attendees:
            if not attendee.email:
                continue

            email = attendee.email.lower()

            # Skip team members
            if email in team_emails:
                continue

            # Only add if domain matches customer domains
            domain = self._extract_domain(email)
            if not domain or domain not in customer_domains:
                continue

            # Check if stakeholder already exists
            stmt = select(DealStakeholder).where(
                col(DealStakeholder.deal_id) == deal.deal_id,
                col(DealStakeholder.email) == email,
            )
            result = await session.execute(stmt)
            if result.scalars().first():
                continue  # Already exists

            # Count existing discovered stakeholders for 5-limit
            count_stmt = select(func.count()).where(
                col(DealStakeholder.deal_id) == deal.deal_id,
                col(DealStakeholder.source) == StakeholderSource.CALENDAR_DISCOVERY,
            )
            count_result = await session.execute(count_stmt)
            current_count = count_result.scalar() or 0

            approval_status = (
                ApprovalStatus.APPROVED
                if current_count < MAX_CALENDAR_DISCOVERY_AUTO_APPROVED
                else ApprovalStatus.PENDING
            )

            # Create stakeholder
            stakeholder = DealStakeholder(
                organisation_id=self.organisation_id,
                integration_id=deal.integration_id,
                deal_id=deal.deal_id,
                email=email,
                name=attendee.display_name,
                source=StakeholderSource.CALENDAR_DISCOVERY,
                approval_status=approval_status,
                stance=StakeholderStance.UNKNOWN,
                temperature=StakeholderTemperature.UNKNOWN,
                engagement_score=0.0,
                last_contact_at=contact_time,
            )
            session.add(stakeholder)
            discovered += 1

            if approval_status == ApprovalStatus.PENDING:
                logger.info(
                    "Calendar discovery limit reached for deal %s, stakeholder %s set to PENDING",
                    deal.deal_id,
                    email,
                )

        return discovered

    async def process_organisation(self) -> dict[str, int]:
        """
        Process all open deals for the organisation.

        Uses fresh session pattern for better connection management:
        - Phase 1: Fresh session for setup (handle cancellations, fetch deals)
        - Phase 2: Fresh session for processing all deals

        Returns:
            Stats dictionary with deals_processed, meetings_linked, and cancelled counts.
        """
        stats = {
            "deals_processed": 0,
            "meetings_linked": 0,
            "cancelled": 0,
        }

        # Phase 1: Setup (fresh session)
        # - Handle cancelled meetings
        # - Fetch all open deals
        deals: list[CRMDeal] = []
        async for session in acquire_org_async_session(self.organisation_id):
            # STEP 0: Handle cancelled meetings FIRST (before matching new ones)
            # This ensures we clean up any previously-mapped meetings that are now cancelled
            cancelled_count = await self._handle_cancelled_associations(session)
            stats["cancelled"] = cancelled_count
            await session.commit()

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
            result = await session.execute(stmt)
            deals = list(result.scalars().all())

        logger.info(f"Processing {len(deals)} open deals for organisation {self.organisation_id}")

        if not deals:
            return stats

        # Phase 2: Process deals (fresh session)
        async for session in acquire_org_async_session(self.organisation_id):
            for deal in deals:
                stats["deals_processed"] += 1
                # Collect customer domains from deal participants
                customer_domains = self._collect_customer_domains(deal)
                team_emails = self._collect_team_emails(deal)

                linked_count = await self._find_and_link_meetings(
                    session, deal, customer_domains, team_emails
                )
                stats["meetings_linked"] += linked_count

            await session.commit()

        return stats

    async def process_deal(self, deal_id: UUID) -> dict[str, int]:
        """
        Process a single deal.

        Requires session to be passed in constructor (used for deal-level tasks).

        Returns:
            Stats dictionary with deals_processed, meetings_linked, and cancelled counts.
        """
        if self._session is None:
            raise ValueError("Session required for process_deal(). Pass session to constructor.")
        session = self._session

        # STEP 0: Handle cancelled meetings FIRST for this deal
        cancelled_count = await self._handle_cancelled_associations(session, deal_id=deal_id)

        deal_participants_rel = cast(Any, CRMDeal.participants)
        stmt = (
            select(CRMDeal)
            .options(selectinload(deal_participants_rel))
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_id) == deal_id,
            )
        )
        result = await session.execute(stmt)
        deal = result.scalars().first()

        if not deal:
            logger.warning(f"Deal {deal_id} not found in org {self.organisation_id}")
            return {"deals_processed": 0, "meetings_linked": 0, "cancelled": 0}

        # Collect customer domains from deal participants
        customer_domains = self._collect_customer_domains(deal)
        team_emails = self._collect_team_emails(deal)

        linked_count = await self._find_and_link_meetings(
            session, deal, customer_domains, team_emails
        )

        await session.commit()

        return {
            "deals_processed": 1,
            "meetings_linked": linked_count,
            "cancelled": cancelled_count,
        }

    async def _find_and_link_meetings(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        customer_domains: set[str],
        team_emails: set[str],
    ) -> int:
        """
        Find matching meetings for a deal and create associations.

        Uses a two-pass approach:
        - Pass 1: Team member is ORGANIZER + at least one contact is attendee (score: 1.0)
        - Pass 2: Team member is ATTENDEE + at least one contact is attendee (score: 0.8)

        In both cases:
        - Event must NOT be cancelled
        - Only ONE contact needs to be present (not all)

        Args:
            session: Database session to use.
            deal: The CRM deal to match meetings for.
            customer_domains: Set of customer email domains (derived from CRM contacts).
            team_emails: Set of team member emails.

        Returns:
            Number of new associations created.
        """
        if not team_emails:
            logger.debug(f"No team emails found for deal {deal.deal_id}")
            return 0

        # Collect all known emails for deal contacts (REJECTED excluded by query)
        contact_emails = await self._collect_contact_emails(session, deal)
        contact_emails -= team_emails  # Remove team emails to avoid self-matching

        # Double safety: also filter by domain in case a stakeholder was just added
        # in this same transaction and hasn't been rejected yet
        internal_domains = await self._collect_internal_domains(session)
        if internal_domains:
            contact_emails = {
                e for e in contact_emails if not self._is_internal_email(e, internal_domains)
            }

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
            session,
            deal,
            team_email_list,
            contact_email_list,
            linked_event_ids,
            customer_domains,
            team_emails,
        )
        linked_count += organizer_count

        # PASS 2: Team member is ATTENDEE only (lower confidence - score 0.8)
        attendee_count = await self._match_events_as_attendee(
            session,
            deal,
            team_email_list,
            contact_email_list,
            linked_event_ids,
            customer_domains,
            team_emails,
        )
        linked_count += attendee_count

        # STEP C: Deactivate associations orphaned by stakeholder cleanup.
        # An association is orphaned if none of the event's attendees match
        # the current (clean) contact_emails set.
        await self._cleanup_orphaned_associations(session, deal, contact_emails)

        logger.info(
            f"Linked {linked_count} meetings for deal {deal.deal_id} "
            f"(organizer={organizer_count}, attendee={attendee_count}, "
            f"team_emails={len(team_emails)}, contact_emails={len(contact_emails)})"
        )

        return linked_count

    async def _match_events_as_organizer(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        team_email_list: list[str],
        contact_email_list: list[str],
        linked_event_ids: set[UUID],
        customer_domains: set[str],
        team_emails: set[str],
    ) -> int:
        """
        Match events where a team member is the ORGANIZER.

        This is the highest confidence match (score: 1.0) because:
        - Team member organized the meeting
        - At least one customer contact is attending

        Args:
            session: Database session to use.
            deal: The CRM deal to match.
            team_email_list: List of team member emails.
            contact_email_list: List of customer contact emails.
            linked_event_ids: Set of already-linked event IDs (will be updated).
            customer_domains: Set of customer email domains for stakeholder discovery.
            team_emails: Set of team member emails for discovery filtering.

        Returns:
            Number of new associations created.
        """
        attendees_relationship = cast(Any, CalendarEvent.attendees)
        user_states_relationship = cast(Any, CalendarEvent.user_states)
        contact_attendee = aliased(CalendarAttendee)

        # Query: Team member is ORGANIZER + contact is attendee + not cancelled
        events_stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_relationship))
            .options(selectinload(user_states_relationship))
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

        events_result = await session.execute(events_stmt)
        matching_events = list(events_result.scalars().all())

        return await self._create_associations(
            session,
            deal,
            matching_events,
            match_score=1.0,
            linked_event_ids=linked_event_ids,
            customer_domains=customer_domains,
            team_emails=team_emails,
        )

    async def _match_events_as_attendee(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        team_email_list: list[str],
        contact_email_list: list[str],
        linked_event_ids: set[UUID],
        customer_domains: set[str],
        team_emails: set[str],
    ) -> int:
        """
        Match events where a team member is an ATTENDEE (but not organizer).

        This is a lower confidence match (score: 0.8) because:
        - Team member is attending but didn't organize
        - At least one customer contact is also attending

        Args:
            session: Database session to use.
            deal: The CRM deal to match.
            team_email_list: List of team member emails.
            contact_email_list: List of customer contact emails.
            linked_event_ids: Set of already-linked event IDs (will be updated).
            customer_domains: Set of customer email domains for stakeholder discovery.
            team_emails: Set of team member emails for discovery filtering.

        Returns:
            Number of new associations created.
        """
        attendees_relationship = cast(Any, CalendarEvent.attendees)
        user_states_relationship = cast(Any, CalendarEvent.user_states)
        team_attendee = aliased(CalendarAttendee)
        contact_attendee = aliased(CalendarAttendee)

        # Query: Team member is ATTENDEE + contact is attendee + not cancelled
        # Exclude events where team member is already the organizer (handled in pass 1)
        events_stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_relationship))
            .options(selectinload(user_states_relationship))
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

        events_result = await session.execute(events_stmt)
        matching_events = list(events_result.scalars().all())

        return await self._create_associations(
            session,
            deal,
            matching_events,
            match_score=0.8,
            linked_event_ids=linked_event_ids,
            customer_domains=customer_domains,
            team_emails=team_emails,
        )

    async def _update_stakeholder_last_contact_from_event(
        self,
        session: AsyncSession,
        deal_id: UUID,
        event: CalendarEvent,
    ) -> int:
        """Update last_contact_at for stakeholders only when the meeting is completed."""
        contact_time = self._resolve_completed_contact_time(event)
        if contact_time is None:
            return 0

        participant_emails = self._collect_event_participant_emails(event)
        if not participant_emails:
            return 0

        stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.is_deleted).is_(False),
            func.lower(DealStakeholder.email).in_(participant_emails),
        )
        result = await session.execute(stmt)
        stakeholders = list(result.scalars().all())

        updated_count = 0
        updated_at = datetime.now(UTC)
        for stakeholder in stakeholders:
            if stakeholder.last_contact_at is None or contact_time > stakeholder.last_contact_at:
                stakeholder.last_contact_at = contact_time
                stakeholder.updated_at = updated_at
                updated_count += 1

        return updated_count

    async def _create_associations(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        events: list[CalendarEvent],
        match_score: float,
        linked_event_ids: set[UUID],
        customer_domains: set[str],
        team_emails: set[str],
    ) -> int:
        """
        Create associations for matched events.

        Args:
            session: Database session to use.
            deal: The CRM deal.
            events: List of matching calendar events.
            match_score: Confidence score for the match (1.0 = organizer, 0.8 = attendee).
            linked_event_ids: Set of already-linked event IDs (will be updated).
            customer_domains: Set of customer email domains for stakeholder discovery.
            team_emails: Set of team member emails for discovery filtering.

        Returns:
            Number of new associations created.
        """
        linked_count = 0

        # Preload integration types for this batch (avoid N+1 queries)
        integration_ids: set[UUID] = set()
        for event in events:
            evt_integration_id = getattr(event, "integration_id", None)
            if evt_integration_id is not None:
                integration_ids.add(evt_integration_id)
            for state in getattr(event, "user_states", None) or []:
                state_integration_id = getattr(state, "integration_id", None)
                if state_integration_id is not None:
                    integration_ids.add(state_integration_id)

        integration_type_by_id: dict[UUID, Any] = {}
        if integration_ids:
            it_stmt = select(
                col(KnowledgeIntegration.integration_id),
                col(KnowledgeIntegration.integration_type),
            ).where(col(KnowledgeIntegration.integration_id).in_(integration_ids))
            it_result = await session.execute(it_stmt)
            integration_type_by_id = {row[0]: row[1] for row in it_result.all()}

        for event in events:
            # Skip if already linked in a previous pass
            if event.event_id in linked_event_ids:
                continue

            # Create new association using INSERT ... ON CONFLICT DO NOTHING
            # This handles race conditions when multiple tasks process the same org
            new_assoc = CalendarEventDealAssociation(
                organisation_id=self.organisation_id,
                event_id=event.event_id,
                deal_id=deal.deal_id,
                status=AssociationStatus.ACTIVE,
                match_score=match_score,
                meeting_prep_status=GenerationStatus.NOT_STARTED,
            )

            stmt = (
                insert(CalendarEventDealAssociation)
                .values(
                    association_id=new_assoc.association_id,
                    organisation_id=new_assoc.organisation_id,
                    event_id=new_assoc.event_id,
                    deal_id=new_assoc.deal_id,
                    status=new_assoc.status,
                    match_score=new_assoc.match_score,
                    meeting_prep_status=new_assoc.meeting_prep_status,
                    meeting_prep_data=None,
                    meeting_summary_status=GenerationStatus.NOT_STARTED,
                    meeting_summary_data=None,
                    meeting_followup_status=GenerationStatus.NOT_STARTED,
                    meeting_followup_data=None,
                    created_at=new_assoc.created_at,
                    updated_at=new_assoc.updated_at,
                )
                .on_conflict_do_nothing(constraint="uq_calendar_event_deal")
            )
            result = await session.execute(stmt)

            # Only proceed if insert succeeded (not a duplicate)
            if (getattr(result, "rowcount", 0) or 0) > 0:
                # Create timeline event inline (same transaction)
                calendar_integration_type = None
                evt_int_id = getattr(event, "integration_id", None)
                if evt_int_id is not None and evt_int_id in integration_type_by_id:
                    calendar_integration_type = integration_type_by_id.get(evt_int_id)
                else:
                    user_states = getattr(event, "user_states", None) or []
                    sorted_states = sorted(
                        (s for s in user_states if getattr(s, "integration_id", None)),
                        key=lambda s: str(s.integration_id),
                    )
                    if sorted_states:
                        calendar_integration_type = integration_type_by_id.get(
                            sorted_states[0].integration_id
                        )

                resolved_source = resolve_meeting_event_source(event, calendar_integration_type)
                timeline_event = build_meeting_timeline_event(
                    organisation_id=self.organisation_id,
                    deal_id=deal.deal_id,
                    association=new_assoc,
                    event=event,
                    event_source=resolved_source,
                )
                session.add(timeline_event)

                linked_count += 1

                # Discover stakeholders from event attendees (only customer domain)
                await self._discover_stakeholders_from_event(
                    session, deal, event, customer_domains, team_emails
                )

            # Always evaluate stakeholder last-contact recency for matched meetings,
            # even when the association already exists from an earlier upcoming run.
            await self._update_stakeholder_last_contact_from_event(
                session=session,
                deal_id=deal.deal_id,
                event=event,
            )
            linked_event_ids.add(event.event_id)

        return linked_count

    async def _cleanup_orphaned_associations(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        contact_emails: set[str],
    ) -> int:
        """Deactivate associations whose events have no current contact attendees.

        After internal stakeholders are rejected and contact_emails recalculated,
        any ACTIVE association where no attendee matches the remaining contacts
        is orphaned (matched only because of a now-rejected stakeholder).
        """
        event_rel = cast(Any, CalendarEventDealAssociation.event)
        attendees_rel = cast(Any, CalendarEvent.attendees)

        stmt = (
            select(CalendarEventDealAssociation)
            .options(selectinload(event_rel).selectinload(attendees_rel))
            .where(
                col(CalendarEventDealAssociation.deal_id) == deal.deal_id,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
            )
        )
        result = await session.execute(stmt)
        associations = list(result.scalars().all())

        count = 0
        for assoc in associations:
            event = assoc.event
            participant_emails = self._collect_event_participant_emails(event)
            if not (participant_emails & contact_emails):
                assoc.status = AssociationStatus.REMOVED
                count += 1
                logger.debug(
                    "Deactivated orphaned association: event_id=%s, deal_id=%s",
                    assoc.event_id,
                    deal.deal_id,
                )

        if count > 0:
            logger.info(
                "Deactivated %d orphaned associations for deal %s",
                count,
                deal.deal_id,
            )

        return count

    async def _handle_cancelled_associations(
        self, session: AsyncSession, deal_id: UUID | None = None
    ) -> int:
        """
        Update associations to CANCELLED status when their events are cancelled.

        This handles the case where a meeting was previously matched but the
        underlying calendar event was later cancelled.

        Note: We use CANCELLED (not REJECTED) because:
        - REJECTED = user explicitly rejected the association
        - CANCELLED = external event (meeting was cancelled)

        Args:
            session: Database session to use.
            deal_id: Optional deal ID to scope the check. If None, checks all deals in org.

        Returns:
            Number of associations updated to CANCELLED.
        """
        # Find associations linked to cancelled events that aren't already cancelled
        stmt = (
            select(CalendarEventDealAssociation)
            .join(
                CalendarEvent,
                col(CalendarEvent.event_id) == col(CalendarEventDealAssociation.event_id),
            )
            .where(
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                col(CalendarEventDealAssociation.status) != AssociationStatus.CANCELLED,
                col(CalendarEvent.status) == "cancelled",
            )
        )

        # Optionally scope to a specific deal
        if deal_id is not None:
            stmt = stmt.where(col(CalendarEventDealAssociation.deal_id) == deal_id)

        result = await session.execute(stmt)
        associations = result.scalars().all()

        updated_count = 0
        for assoc in associations:
            assoc.status = AssociationStatus.CANCELLED
            updated_count += 1
            logger.info(
                f"Marked cancelled event association: event_id={assoc.event_id}, "
                f"deal_id={assoc.deal_id}"
            )

        if updated_count > 0:
            logger.info(
                f"Marked {updated_count} associations as CANCELLED "
                f"(org={self.organisation_id}, deal={deal_id})"
            )

        return updated_count


async def process_deal_meetings(
    organisation_id: UUID,
) -> dict[str, int]:
    """
    Process deal meetings for an organisation.

    Uses fresh session pattern - no session needed from caller.

    Args:
        organisation_id: UUID of the organisation

    Returns:
        Dictionary with processing statistics.
    """
    matcher = DealMeetingMatcher(organisation_id)
    return await matcher.process_organisation()
