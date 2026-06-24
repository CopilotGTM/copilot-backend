"""Deal-to-calendar meeting matcher and post-commit follow-up dispatch.

The matcher links CRM deals to calendar events inside an org-scoped transaction.
It also records meeting-bot send intent, but Celery dispatch is delayed until
after commit so downstream workers never observe rolled-back associations.
"""

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
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.timeline.deal_timeline_sync_service import build_meeting_timeline_event
from src.service.crm.timeline.timeline_event_source_resolver import resolve_meeting_event_source
from src.service.meeting_bot.recording_window import is_within_recording_dispatch_window
from src.service.meeting_bot.session_service import (
    MeetingRecordingSessionService,
)
from src.workers.deal_worker.client import send_deal_task_async
from src.workers.deal_worker.services.deal_mapper_services.integration_email_resolver import (
    CALENDAR_MATCHER_INTEGRATION_TYPES,
    get_active_integration_emails_for_users,
    get_active_integration_emails_grouped_by_user,
)

logger = get_logger(__name__)

# Max auto-approved discovered stakeholders per deal from calendar events
MAX_CALENDAR_DISCOVERY_AUTO_APPROVED = 5


class DealMeetingMatcher:
    """Service to match calendar events to CRM deals based on owner and contact participation."""

    def __init__(
        self,
        organisation_id: UUID,
        session: AsyncSession | None = None,
        *,
        suppress_research_dispatch: bool = False,
    ):
        """
        Initialize the matcher with organisation context.

        Args:
            organisation_id: UUID of the organisation
            session: Optional async database session. If not provided,
                     process_organisation() will acquire fresh sessions.
                     Required for process_deal().
            suppress_research_dispatch: When True, discovered stakeholders are
                     persisted but research is not auto-dispatched.
        """
        self.organisation_id = organisation_id
        self._session = session  # Optional - used by process_deal()
        self._suppress_research_dispatch = suppress_research_dispatch
        self._contact_cache_by_id: dict[UUID, CRMContact] = {}
        self._internal_domains: set[str] | None = None  # Lazy-loaded cache
        self._pending_research: list[tuple[UUID, UUID]] = []  # (participant_id, deal_id)
        # Recording rows are created inside the DB transaction, but the Celery
        # dispatch happens only after commit so workers never see rolled-back rows.
        self._pending_recording_event_ids: set[UUID] = set()

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
        return contact

    async def _collect_contact_emails(self, session: AsyncSession, deal: CRMDeal) -> set[str]:
        """Collect all active approved contact participant emails for a deal."""
        emails: set[str] = set()

        for participant in self._get_participants(deal):
            if participant.participant_type != CRMParticipantType.CONTACT:
                continue
            if participant.is_deleted:
                continue
            if participant.approval_status != ApprovalStatus.APPROVED.value:
                continue
            normalized = self._normalize_email(participant.related_email)
            if normalized:
                emails.add(normalized)
            if participant.related_local_id:
                contact = await self._get_contact_by_id(session, participant.related_local_id)
                self._add_contact_emails(contact, emails)

        return emails

    def _collect_participant_user_ids(self, deal: CRMDeal) -> set[UUID]:
        """Collect internal user ids that can own connected calendar integrations."""
        participant_user_ids: set[UUID] = set()

        if deal.owner_id:
            participant_user_ids.add(deal.owner_id)

        for participant in self._get_participants(deal):
            if participant.participant_type != CRMParticipantType.USER:
                continue
            if participant.related_local_id:
                participant_user_ids.add(participant.related_local_id)

        return participant_user_ids

    async def _collect_team_emails(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        *,
        integration_emails_by_user: dict[UUID, set[str]] | None = None,
    ) -> set[str]:
        """Collect all team member (USER participant) emails for a deal.

        This includes all users with roles like owner, collaborator, split_owner, etc.
        Any team member's participation in a meeting qualifies it for matching.

        Args:
            deal: The CRM deal to collect team emails for.

        Returns:
            Set of normalized email addresses for all team members.
        """
        emails: set[str] = set()
        participant_user_ids = self._collect_participant_user_ids(deal)

        owner_email = self._normalize_email(deal.owner_email)
        if owner_email:
            emails.add(owner_email)

        for participant in self._get_participants(deal):
            if participant.participant_type != CRMParticipantType.USER:
                continue

            normalized = self._normalize_email(participant.related_email)
            if normalized:
                emails.add(normalized)

        # Expand active user-owned calendar integrations.
        if participant_user_ids:
            if integration_emails_by_user is None:
                integration_emails = await get_active_integration_emails_for_users(
                    session,
                    organisation_id=self.organisation_id,
                    created_by_user_ids=participant_user_ids,
                    integration_types=CALENDAR_MATCHER_INTEGRATION_TYPES,
                )
            else:
                integration_emails = {
                    email
                    for user_id in participant_user_ids
                    for email in integration_emails_by_user.get(user_id, set())
                }
            emails.update(integration_emails)

        logger.info(
            "Resolved deal meeting matcher team emails for deal %s",
            deal.deal_id,
            extra={
                "event": "deal_meeting_team_emails_resolved",
                "organisation_id": str(self.organisation_id),
                "deal_id": str(deal.deal_id),
                "participant_user_ids": sorted(str(user_id) for user_id in participant_user_ids),
                "team_email_count": len(emails),
                "team_emails": sorted(emails),
            },
        )

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

    def _normalize_company_domain(self, domain: str | None) -> str | None:
        """Normalize a company domain from user-facing fields."""
        if not domain:
            return None

        cleaned = domain.strip().lower()
        if not cleaned:
            return None

        if cleaned.startswith("http://"):
            cleaned = cleaned[len("http://") :]
        elif cleaned.startswith("https://"):
            cleaned = cleaned[len("https://") :]

        if cleaned.startswith("www."):
            cleaned = cleaned[len("www.") :]

        cleaned = cleaned.split("/")[0].split("?")[0].split("#")[0]
        return cleaned or None

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
            if participant.is_deleted:
                continue
            if participant.approval_status != ApprovalStatus.APPROVED.value:
                continue
            if participant.related_email:
                domain = self._extract_domain(participant.related_email)
                if domain:
                    domains.add(domain)

        account_domain = self._normalize_company_domain(deal.company_domain)
        if account_domain:
            domains.add(account_domain)

        return domains

    async def _discover_from_existing_associations(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        customer_domains: set[str],
        team_emails: set[str],
    ) -> int:
        """Re-scan already-associated meetings for undiscovered stakeholders.

        Runs every cycle to catch attendees whose domain became known after
        the meeting was first associated (e.g., new stakeholder added, new
        CRM contact synced). The per-event discovery method is idempotent —
        it skips attendees who are already stakeholders.
        """
        attendees_rel = cast(Any, CalendarEvent.attendees)
        stmt = (
            select(CalendarEvent)
            .options(selectinload(attendees_rel))
            .join(
                CalendarEventDealAssociation,
                col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
            )
            .where(
                col(CalendarEventDealAssociation.deal_id) == deal.deal_id,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
            )
        )
        result = await session.execute(stmt)
        events = list(result.scalars().all())

        total_discovered = 0
        for event in events:
            discovered = await self._discover_stakeholders_from_event(
                session, deal, event, customer_domains, team_emails
            )
            total_discovered += discovered

        return total_discovered

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
        if not event.attendees:
            return 0

        discovered = 0
        contact_time = self._resolve_completed_contact_time(event)

        if not customer_domains:
            attendee_domains = sorted(
                {
                    domain
                    for attendee in event.attendees
                    if attendee.email
                    and (domain := self._extract_domain(attendee.email)) is not None
                }
            )
            logger.info(
                "Calendar stakeholder discovery skipped for deal %s because customer domains are empty",
                deal.deal_id,
                extra={
                    "event": "deal_meeting_discovery_no_customer_domains",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal.deal_id),
                    "event_id": str(event.event_id),
                    "attendee_count": len(event.attendees),
                    "attendee_domains": attendee_domains,
                },
            )
            return 0

        skipped_domain_mismatch: dict[str, int] = {}

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
                skipped_domain_mismatch[domain or "missing"] = (
                    skipped_domain_mismatch.get(domain or "missing", 0) + 1
                )
                continue

            existing = await self._find_contact_participant_by_email(
                session,
                deal_id=deal.deal_id,
                email=email,
            )
            if existing:
                continue

            # Count existing discovered stakeholders for 5-limit
            current_count = await self._count_discovered_participants(
                session,
                deal_id=deal.deal_id,
                source=StakeholderSource.CALENDAR_DISCOVERY,
            )

            approval_status = (
                ApprovalStatus.APPROVED
                if current_count < MAX_CALENDAR_DISCOVERY_AUTO_APPROVED
                else ApprovalStatus.PENDING
            )

            # Link to existing CRMContact if one exists
            contact_stmt = select(col(CRMContact.contact_id)).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.email) == email,
            )
            contact_result = await session.execute(contact_stmt)
            found_contact_id = contact_result.scalar_one_or_none()

            if deal.integration_id is None:
                logger.warning(
                    "Skipping calendar-discovered participant without deal integration",
                    extra={
                        "event": "deal_meeting_discovery_missing_integration",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal.deal_id),
                        "attendee_email": email,
                    },
                )
                continue

            participant = CRMDealParticipant(
                organisation_id=self.organisation_id,
                integration_id=deal.integration_id,
                deal_id=deal.deal_id,
                participant_type=CRMParticipantType.CONTACT,
                related_local_id=found_contact_id,
                related_name=attendee.display_name,
                related_email=email,
                participant_source=StakeholderSource.CALENDAR_DISCOVERY.value,
                approval_status=approval_status.value,
                stance=StakeholderStance.UNKNOWN.value,
                temperature=StakeholderTemperature.UNKNOWN.value,
                last_contact_at=contact_time,
            )
            session.add(participant)
            await session.flush()
            self._pending_research.append((participant.participant_id, deal.deal_id))
            discovered += 1

            if approval_status == ApprovalStatus.PENDING:
                logger.info(
                    "Calendar discovery limit reached for deal %s, stakeholder %s set to PENDING",
                    deal.deal_id,
                    email,
                    extra={
                        "event": "deal_meeting_discovery_limit_reached",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal.deal_id),
                        "attendee_email": email,
                        "approval_status": approval_status.value,
                    },
                )

        if discovered == 0 and skipped_domain_mismatch:
            logger.info(
                "Calendar stakeholder discovery found no eligible attendees for deal %s",
                deal.deal_id,
                extra={
                    "event": "deal_meeting_discovery_domain_mismatch",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal.deal_id),
                    "event_id": str(event.event_id),
                    "customer_domains": sorted(customer_domains),
                    "skipped_domain_counts": skipped_domain_mismatch,
                },
            )

        return discovered

    async def _find_contact_participant_by_email(
        self,
        session: AsyncSession,
        *,
        deal_id: UUID,
        email: str,
    ) -> CRMDealParticipant | None:
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            func.lower(func.trim(col(CRMDealParticipant.related_email))) == email,
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def _count_discovered_participants(
        self,
        session: AsyncSession,
        *,
        deal_id: UUID,
        source: StakeholderSource,
    ) -> int:
        stmt = select(func.count()).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.participant_source) == source.value,
        )
        result = await session.execute(stmt)
        return result.scalar() or 0

    async def _dispatch_pending_research(self) -> None:
        """Dispatch research for stakeholders queued during sync."""
        if not self._pending_research:
            return
        if self._suppress_research_dispatch:
            self._pending_research.clear()
            return
        try:
            from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
                dispatch_stakeholder_research_async,
            )

            for stakeholder_id, deal_id in self._pending_research:
                try:
                    await dispatch_stakeholder_research_async(
                        organisation_id=self.organisation_id,
                        stakeholder_id=stakeholder_id,
                        deal_id=deal_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to dispatch research for stakeholder %s",
                        stakeholder_id,
                        exc_info=True,
                        extra={
                            "event": "deal_meeting_research_dispatch_failed",
                            "organisation_id": str(self.organisation_id),
                            "stakeholder_id": str(stakeholder_id),
                            "deal_id": str(deal_id),
                        },
                    )
        finally:
            self._pending_research.clear()

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

        logger.info(
            "Processing %d open deals for organisation %s",
            len(deals),
            self.organisation_id,
            extra={
                "event": "deal_meeting_process_organisation_started",
                "organisation_id": str(self.organisation_id),
                "open_deals": len(deals),
            },
        )

        if not deals:
            logger.info(
                "Preloaded deal meeting matcher integration emails for 0 participant users",
                extra={
                    "event": "deal_meeting_integration_emails_preloaded",
                    "organisation_id": str(self.organisation_id),
                    "participant_user_count": 0,
                    "integration_email_user_count": 0,
                    "integration_email_count": 0,
                },
            )
            return stats

        # Phase 2: Process deals (fresh session)
        async for session in acquire_org_async_session(self.organisation_id):
            participant_user_ids = set()
            for deal in deals:
                participant_user_ids.update(self._collect_participant_user_ids(deal))

            integration_emails_by_user = await get_active_integration_emails_grouped_by_user(
                session=session,
                organisation_id=self.organisation_id,
                created_by_user_ids=participant_user_ids,
                integration_types=CALENDAR_MATCHER_INTEGRATION_TYPES,
            )
            logger.info(
                "Preloaded deal meeting matcher integration emails for %d participant users",
                len(participant_user_ids),
                extra={
                    "event": "deal_meeting_integration_emails_preloaded",
                    "organisation_id": str(self.organisation_id),
                    "participant_user_count": len(participant_user_ids),
                    "integration_email_user_count": len(integration_emails_by_user),
                    "integration_email_count": sum(
                        len(emails) for emails in integration_emails_by_user.values()
                    ),
                },
            )

            for deal in deals:
                stats["deals_processed"] += 1
                # Collect customer domains from deal participants
                customer_domains = self._collect_customer_domains(deal)
                team_emails = await self._collect_team_emails(
                    session,
                    deal,
                    integration_emails_by_user=integration_emails_by_user,
                )

                linked_count = await self._find_and_link_meetings(
                    session, deal, customer_domains, team_emails
                )
                stats["meetings_linked"] += linked_count

            await session.commit()
            # The recording send task depends on the association/session rows
            # being durable before another worker starts reading them.
            await self._dispatch_pending_recordings()

        await self._dispatch_pending_research()
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
            logger.warning(
                "Deal %s not found in org %s",
                deal_id,
                self.organisation_id,
                extra={
                    "event": "deal_meeting_process_deal_not_found",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                },
            )
            return {"deals_processed": 0, "meetings_linked": 0, "cancelled": 0}

        # Collect customer domains from deal participants
        customer_domains = self._collect_customer_domains(deal)
        team_emails = await self._collect_team_emails(session, deal)

        linked_count = await self._find_and_link_meetings(
            session, deal, customer_domains, team_emails
        )

        await session.commit()
        # Deal-scoped processing uses the same post-commit rule as org-wide runs.
        await self._dispatch_pending_recordings()
        await self._dispatch_pending_research()

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

        # STEP C: Discover stakeholders from already-associated meetings.
        # Handles the case where customer_domains expanded since events were first
        # associated (e.g., new stakeholder added, new CRM contact synced).
        discovered = 0
        if customer_domains:
            discovered = await self._discover_from_existing_associations(
                session, deal, customer_domains, team_emails
            )

        # STEP D: Deactivate associations orphaned by stakeholder cleanup.
        # An association is orphaned if none of the event's attendees match
        # the current (clean) contact_emails set.
        await self._cleanup_orphaned_associations(session, deal, contact_emails)

        logger.info(
            "Linked %d meetings for deal %s (organizer=%d, attendee=%d, "
            "discovered=%d, team_emails=%d, contact_emails=%d)",
            linked_count,
            deal.deal_id,
            organizer_count,
            attendee_count,
            discovered,
            len(team_emails),
            len(contact_emails),
            extra={
                "event": "deal_meeting_linking_completed",
                "organisation_id": str(self.organisation_id),
                "deal_id": str(deal.deal_id),
                "linked_count": linked_count,
                "organizer_count": organizer_count,
                "attendee_count": attendee_count,
                "discovered": discovered,
                "team_emails": len(team_emails),
                "contact_emails": len(contact_emails),
            },
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

        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
            func.lower(CRMDealParticipant.related_email).in_(participant_emails),
        )
        result = await session.execute(stmt)
        participants = list(result.scalars().all())

        updated_count = 0
        updated_at = datetime.now(UTC)
        for participant in participants:
            if participant.last_contact_at is None or contact_time > participant.last_contact_at:
                participant.last_contact_at = contact_time
                participant.updated_at = updated_at
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

            # Association insert is conflict-safe because multiple matcher passes can
            # process the same org while calendar sync is still catching up.
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
                # Timeline writes share the association transaction so the CRM view
                # never shows a linked meeting without its corresponding activity.
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
                # Projection happens before commit with the association insert.
                # Replay stays idempotent because the association ID is the feed ID.
                await ActivityFeedProjectionService(
                    session,
                    self.organisation_id,
                ).upsert_meeting_association(new_assoc, event)
                # Queue the provider send only for meetings we actually linked to
                # a deal in this transaction.
                await self._enqueue_meeting_recording(session, event)

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

    async def _enqueue_meeting_recording(
        self,
        session: AsyncSession,
        event: CalendarEvent,
    ) -> bool:
        """Persist a pending recording row for future meetings with a join URL.

        Uses `ON CONFLICT DO NOTHING` because multiple matcher passes can touch
        the same event before the recording dispatch task runs. When the row already
        exists, a stale join target still queues dispatch so Recall can be resynced.
        """
        meeting_url = getattr(event, "conference_link", None)
        if not meeting_url:
            return False

        join_at = getattr(event, "start_at", None)
        if join_at is None:
            return False
        if join_at.tzinfo is None:
            join_at = join_at.replace(tzinfo=UTC)
        if not is_within_recording_dispatch_window(join_at, datetime.now(UTC)):
            return False

        stmt = (
            insert(MeetingRecordingSession)
            .values(
                organisation_id=self.organisation_id,
                calendar_event_id=event.event_id,
                provider=MeetingRecordingProvider.RECALL_AI.value,
                dedupe_key=MeetingRecordingSessionService.build_dedupe_key(
                    self.organisation_id,
                    event.event_id,
                ),
                meeting_url=meeting_url,
                join_at=join_at,
                status=MeetingRecordingStatus.PENDING_SEND.value,
                transcript_status=MeetingTranscriptStatus.NOT_REQUESTED.value,
            )
            .on_conflict_do_nothing(constraint="uq_meeting_recording_org_event_provider")
        )
        result = await session.execute(stmt)
        created = (getattr(result, "rowcount", 0) or 0) > 0
        should_enqueue = created
        if not created:
            # Existing rows are normally idempotent duplicates. A moved meeting is the
            # exception: it needs the same send task so dispatch can cancel/recreate.
            existing_session = await self._get_existing_recording_session(session, event.event_id)
            should_enqueue = bool(
                existing_session
                and self._is_recording_session_stale(
                    existing_session,
                    meeting_url=meeting_url,
                    join_at=join_at,
                )
            )

        if should_enqueue:
            # Store only the event id here; the send task re-reads durable DB
            # state after commit instead of relying on in-memory event objects.
            self._pending_recording_event_ids.add(event.event_id)
        return created

    async def _get_existing_recording_session(
        self,
        session: AsyncSession,
        calendar_event_id: UUID,
    ) -> MeetingRecordingSession | None:
        """Read the durable recording row for one org/event/provider tuple.

        The org filter is the tenant boundary here; a calendar event id alone is not
        trusted when deciding whether to enqueue provider work.
        """
        stmt = select(MeetingRecordingSession).where(
            col(MeetingRecordingSession.organisation_id) == self.organisation_id,
            col(MeetingRecordingSession.calendar_event_id) == calendar_event_id,
            col(MeetingRecordingSession.provider) == MeetingRecordingProvider.RECALL_AI.value,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _normalize_utc(value: datetime) -> datetime:
        """Normalize event timestamps for consistent comparison."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _is_recording_session_stale(
        session: MeetingRecordingSession,
        *,
        meeting_url: str,
        join_at: datetime,
    ) -> bool:
        """Detect whether an existing session needs a provider resync.

        Matcher only decides whether to queue work. The dispatch service owns the
        actual reset/cancel/send sequence after the transaction commits.
        """
        existing_join_at = getattr(session, "join_at", None)
        existing_status = getattr(session, "status", None)
        if existing_status is not None and hasattr(existing_status, "value"):
            existing_status = existing_status.value
        if str(existing_status) == MeetingRecordingStatus.CANCELLED.value:
            return False
        if existing_join_at is None:
            return False
        return (
            DealMeetingMatcher._normalize_utc(existing_join_at)
            != DealMeetingMatcher._normalize_utc(join_at)
            or getattr(session, "meeting_url", None) != meeting_url
        )

    async def _dispatch_pending_recordings(self) -> None:
        """Dispatch recording send tasks after the transaction commits."""
        if not self._pending_recording_event_ids:
            return

        pending_event_ids = sorted(self._pending_recording_event_ids, key=str)
        self._pending_recording_event_ids.clear()

        for event_id in pending_event_ids:
            try:
                await send_deal_task_async(
                    "deal.send_meeting_recording",
                    kwargs={
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(event_id),
                    },
                    queue="deal",
                )
            except Exception:
                logger.error(
                    "Failed to dispatch meeting recording send task",
                    extra={
                        "event": "meeting_recording_dispatch_failed",
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(event_id),
                    },
                    exc_info=True,
                )

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
                # Removing the association should hide the card from every feed
                # link it created, while leaving the calendar event itself intact.
                await ActivityFeedProjectionService(
                    session,
                    self.organisation_id,
                ).soft_delete_activity_projection(assoc.association_id)
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
                extra={
                    "event": "deal_meeting_orphaned_associations_deactivated",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal.deal_id),
                    "orphaned_associations": count,
                },
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
            # Cancelled meetings are not deleted from calendar history, but the
            # active deal feed should stop showing their projected activity card.
            await ActivityFeedProjectionService(
                session,
                self.organisation_id,
            ).soft_delete_activity_projection(assoc.association_id)
            updated_count += 1
            logger.info(
                "Marked cancelled event association: event_id=%s, deal_id=%s",
                assoc.event_id,
                assoc.deal_id,
                extra={
                    "event": "deal_meeting_association_cancelled",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(assoc.deal_id),
                    "event_id": str(assoc.event_id),
                    "association_id": str(assoc.association_id),
                },
            )

        if updated_count > 0:
            logger.info(
                "Marked %d associations as CANCELLED (org=%s, deal=%s)",
                updated_count,
                self.organisation_id,
                deal_id,
                extra={
                    "event": "deal_meeting_cancelled_associations_completed",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id) if deal_id else None,
                    "cancelled_associations": updated_count,
                },
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
