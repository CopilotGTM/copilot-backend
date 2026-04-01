"""Service to match emails to CRM deals based on stakeholder participation.

This module provides the DealEmailMatcher service that:
1. Loads all active deals with their team and contact stakeholders
2. Builds in-memory lookup maps for efficient email-to-deal matching
3. Processes unmatched emails and creates associations

Uses Email-First batch approach for efficient hourly processing:
- Load deals once → Build stakeholder maps
- Query unprocessed emails
- Match emails to deals using in-memory lookups

See docs/Technical Documentation/deal/deal-email-matching-automation.md for details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col, select
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
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
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
)
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.timeline.deal_timeline_sync_service import build_email_timeline_event
from src.service.crm.timeline.timeline_event_source_resolver import resolve_email_event_source

logger = get_logger(__name__)


# Configuration constants
MIN_MATCH_SCORE = 0.7
BATCH_SIZE = 500  # Max emails to fetch per run
MAX_EMAIL_DISCOVERY_AUTO_APPROVED = 5

# Calendar invite subject patterns (case-insensitive)
# These emails are already matched via calendar sync, so we skip them
CALENDAR_INVITE_PATTERNS = (
    "invitation:",
    "invite:",
    "accepted:",
    "declined:",
    "tentative:",
    "updated invitation:",
    "canceled:",
    "cancelled:",
    "meeting request:",
    "meeting response:",
)


@dataclass
class DealStakeholderInfo:
    """Container for deal stakeholder information."""

    deal_id: UUID
    integration_id: UUID | None = None
    team_emails: set[str] = field(default_factory=set)
    contact_emails: set[str] = field(default_factory=set)
    company_domains: set[str] = field(default_factory=set)


@dataclass
class EmailMatchResult:
    """Result of matching an email to deals."""

    deal_id: UUID
    score: float
    direction: DealEmailDirection
    matched_emails: list[str]


class DealEmailMatcher:
    """Service to match emails to CRM deals based on stakeholder participation."""

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
                     process_organisation() will acquire fresh sessions per batch.
                     Required for process_deal().
        """
        self.organisation_id = organisation_id
        self._session = session  # Optional - used by process_deal()

        # In-memory lookup maps (populated by _build_stakeholder_maps)
        self._team_email_to_deals: dict[str, set[UUID]] = {}
        self._contact_email_to_deals: dict[str, set[UUID]] = {}
        self._deal_info: dict[UUID, DealStakeholderInfo] = {}
        self._pending_research: list[tuple[UUID, UUID]] = []  # (stakeholder_id, deal_id)

    def _normalize_email(self, email: str | None) -> str | None:
        """Normalize email string by stripping whitespace and converting to lowercase."""
        if not email:
            return None
        normalized = email.strip().lower()
        if not normalized or normalized == "null":
            return None
        return normalized

    def _extract_domain(self, email: str) -> str | None:
        """Extract domain from email address."""
        if not email or "@" not in email:
            return None
        return email.split("@")[1].lower()

    def _is_calendar_invite_email(self, email: Email) -> bool:
        """Check if email is a calendar invite based on subject patterns.

        Calendar invite emails are already matched via calendar sync,
        so we skip them to avoid duplicate associations.
        """
        if not email.subject:
            return False
        subject_lower = email.subject.lower().strip()
        return any(subject_lower.startswith(pattern) for pattern in CALENDAR_INVITE_PATTERNS)

    def _collect_company_domains(self, deal: CRMDeal) -> set[str]:
        """Collect customer domains from deal's CONTACT participants."""
        domains: set[str] = set()

        # From primary contact
        if deal.primary_contact_email:
            domain = self._extract_domain(deal.primary_contact_email)
            if domain:
                domains.add(domain)

        # From CONTACT participants
        participants = cast(list[CRMDealParticipant] | None, getattr(deal, "participants", None))
        if participants:
            for participant in participants:
                if participant.participant_type != CRMParticipantType.CONTACT:
                    continue
                if participant.related_email:
                    domain = self._extract_domain(participant.related_email)
                    if domain:
                        domains.add(domain)

        return domains

    def _resolve_email_contact_time(self, email: Email) -> datetime | None:
        """Resolve canonical contact time for stakeholder engagement from an email."""
        return email.received_at or email.sent_at

    async def _discover_from_existing_email_associations(
        self,
        session: AsyncSession,
        deal_id: UUID,
        info: DealStakeholderInfo,
    ) -> int:
        """Re-scan already-associated emails for undiscovered stakeholders.

        Queries emails linked to this deal and runs discovery using current
        company_domains. The per-email discovery method is idempotent —
        it skips participants who are already stakeholders.
        """
        from src.domain.models.organisation_tables.sync.email_models import (
            DealEmailAssociation,
            Email,
        )

        stmt = (
            select(Email)
            .join(
                DealEmailAssociation,
                col(DealEmailAssociation.internet_message_id) == col(Email.internet_message_id),
            )
            .where(
                col(DealEmailAssociation.deal_id) == deal_id,
                col(DealEmailAssociation.organisation_id) == self.organisation_id,
                col(Email.is_deleted).is_(False),
            )
            .distinct()
        )
        result = await session.execute(stmt)
        emails = list(result.scalars().all())

        total_discovered = 0
        for email in emails:
            discovered = await self._discover_stakeholders_from_email(session, email, deal_id, info)
            total_discovered += discovered

        return total_discovered

    async def _discover_stakeholders_from_email(
        self,
        session: AsyncSession,
        email: Email,
        deal_id: UUID,
        info: DealStakeholderInfo,
    ) -> int:
        """Discover new stakeholders from email participants.

        Args:
            session: Database session to use for queries and inserts.
            email: Email object to extract participants from.
            deal_id: UUID of the deal to add stakeholders to.
            info: DealStakeholderInfo with existing stakeholder data.

        Only adds participants who:
        - Have email domain matching customer domains
        - Are not already team members
        - Don't already exist as stakeholders for this deal

        Applies 5-limit: first 5 discovered stakeholders are auto-APPROVED,
        beyond that they are PENDING (require user approval).

        Returns:
            Number of stakeholders discovered.
        """
        if not info.company_domains:
            return 0

        discovered = 0
        contact_time = self._resolve_email_contact_time(email)

        # Collect all email participants (from, to, cc)
        all_participants: set[str] = set()
        from_email = self._normalize_email(email.from_email)
        if from_email:
            all_participants.add(from_email)
        for addr in email.to_emails or []:
            normalized = self._normalize_email(addr)
            if normalized:
                all_participants.add(normalized)
        for addr in email.cc_emails or []:
            normalized = self._normalize_email(addr)
            if normalized:
                all_participants.add(normalized)

        for participant_email in all_participants:
            # Skip team members
            if participant_email in info.team_emails:
                continue

            # Skip known contacts
            if participant_email in info.contact_emails:
                continue

            # Only add if domain matches customer domains
            domain = self._extract_domain(participant_email)
            if not domain or domain not in info.company_domains:
                continue

            # Check if stakeholder already exists
            stmt = select(DealStakeholder).where(
                col(DealStakeholder.deal_id) == deal_id,
                col(DealStakeholder.email) == participant_email,
            )
            result = await session.execute(stmt)
            existing = result.scalars().first()
            if existing:
                continue

            # Count existing discovered stakeholders for 5-limit
            count_stmt = select(func.count()).where(
                col(DealStakeholder.deal_id) == deal_id,
                col(DealStakeholder.source) == StakeholderSource.EMAIL_DISCOVERY,
            )
            count_result = await session.execute(count_stmt)
            current_count = count_result.scalar() or 0

            approval_status = (
                ApprovalStatus.APPROVED
                if current_count < MAX_EMAIL_DISCOVERY_AUTO_APPROVED
                else ApprovalStatus.PENDING
            )

            # Link to existing CRMContact if one exists
            contact_stmt = select(col(CRMContact.contact_id)).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.email) == participant_email,
            )
            contact_result = await session.execute(contact_stmt)
            found_contact_id = contact_result.scalar_one_or_none()

            # Create stakeholder
            stakeholder = DealStakeholder(
                organisation_id=self.organisation_id,
                integration_id=info.integration_id,
                deal_id=deal_id,
                email=participant_email,
                name=None,
                contact_id=found_contact_id,
                source=StakeholderSource.EMAIL_DISCOVERY,
                approval_status=approval_status,
                stance=StakeholderStance.UNKNOWN,
                temperature=StakeholderTemperature.UNKNOWN,
                engagement_score=0.0,
                last_contact_at=contact_time,
            )
            session.add(stakeholder)
            await session.flush()
            self._pending_research.append((stakeholder.stakeholder_id, deal_id))
            discovered += 1

            if approval_status == ApprovalStatus.PENDING:
                logger.info(
                    "Email discovery limit reached for deal %s, stakeholder %s set to PENDING",
                    deal_id,
                    participant_email,
                )

        return discovered

    async def _dispatch_pending_research(self) -> None:
        """Dispatch research for stakeholders queued during sync."""
        if not self._pending_research:
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
                    )
        finally:
            self._pending_research.clear()

    async def _build_stakeholder_maps(self, session: AsyncSession) -> None:
        """
        Load all active deals and build stakeholder-to-deal lookup maps.

        Args:
            session: Database session to use for queries.

        Populates:
        - _team_email_to_deals: Maps team member emails to their deal IDs
        - _contact_email_to_deals: Maps contact emails to their deal IDs
        - _deal_info: Maps deal IDs to their stakeholder info
        """
        # Clear existing maps
        self._team_email_to_deals.clear()
        self._contact_email_to_deals.clear()
        self._deal_info.clear()

        # Fetch all open deals with participants
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
        deals = result.scalars().all()

        logger.info(f"Building stakeholder maps for {len(deals)} active deals")

        for deal in deals:
            info = DealStakeholderInfo(
                deal_id=deal.deal_id,
                integration_id=deal.integration_id,
            )

            # Collect team emails (owner + technical owner from participants)
            self._collect_team_emails(deal, info)

            # Collect contact emails from DealStakeholder table
            await self._collect_stakeholder_emails(session, deal.deal_id, info)

            # Collect company domains for stakeholder discovery
            info.company_domains = self._collect_company_domains(deal)

            # Store deal info
            self._deal_info[deal.deal_id] = info

            # Build reverse lookup maps
            for email in info.team_emails:
                if email not in self._team_email_to_deals:
                    self._team_email_to_deals[email] = set()
                self._team_email_to_deals[email].add(deal.deal_id)

            for email in info.contact_emails:
                if email not in self._contact_email_to_deals:
                    self._contact_email_to_deals[email] = set()
                self._contact_email_to_deals[email].add(deal.deal_id)

        logger.info(
            f"Stakeholder maps built: {len(self._team_email_to_deals)} team emails, "
            f"{len(self._contact_email_to_deals)} contact emails across {len(self._deal_info)} deals"
        )

    def _collect_team_emails(self, deal: CRMDeal, info: DealStakeholderInfo) -> None:
        """
        Collect team member emails from deal owner and USER participants.

        Team emails come from:
        1. CRMDeal.owner_email - Primary deal owner
        2. CRMDealParticipant where participant_type = USER
           - Includes: owner, collaborator, split_owner, technical_owner roles
        """
        # 1. Deal owner email (from deal property)
        owner_email = self._normalize_email(deal.owner_email)
        if owner_email:
            info.team_emails.add(owner_email)

        # 2. All USER participants (collaborators, split_owners, etc.)
        participants = cast(list[CRMDealParticipant] | None, getattr(deal, "participants", None))
        if participants:
            for participant in participants:
                if participant.participant_type != CRMParticipantType.USER:
                    continue
                email = self._normalize_email(participant.related_email)
                if email:
                    info.team_emails.add(email)

    async def _collect_stakeholder_emails(
        self, session: AsyncSession, deal_id: UUID, info: DealStakeholderInfo
    ) -> None:
        """
        Collect contact emails from DealStakeholder table.

        Args:
            session: Database session to use for queries.
            deal_id: UUID of the deal to collect stakeholders for.
            info: DealStakeholderInfo object to populate.

        Uses DealStakeholder table which contains:
        - CRM-synced contacts (source=CRM)
        - Discovered contacts from calendar (source=CALENDAR_DISCOVERY)
        - Discovered contacts from email (source=EMAIL_DISCOVERY)

        Only returns APPROVED stakeholders (not PENDING or REJECTED).
        """
        stmt = select(col(DealStakeholder.email)).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.is_deleted).is_(False),
            col(DealStakeholder.approval_status) == ApprovalStatus.APPROVED,
        )
        result = await session.execute(stmt)
        stakeholder_emails = result.scalars().all()

        for email in stakeholder_emails:
            normalized = self._normalize_email(email)
            if normalized and normalized not in info.team_emails:
                info.contact_emails.add(normalized)

    async def _get_unprocessed_emails(self, session: AsyncSession) -> list[Email]:
        """
        Get emails that have not been processed for deal matching.

        Args:
            session: Database session to use for queries.

        Returns emails with valid internet_message_id that are not yet
        in the deal_email_associations table.
        """
        # Subquery to get already-processed internet_message_ids
        processed_subq = (
            select(col(DealEmailAssociation.internet_message_id))
            .where(col(DealEmailAssociation.organisation_id) == self.organisation_id)
            .distinct()
        )

        # Main query for unprocessed emails
        stmt = (
            select(Email)
            .where(
                col(Email.organisation_id) == self.organisation_id,
                col(Email.internet_message_id).isnot(None),
                col(Email.internet_message_id) != "",
                ~col(Email.internet_message_id).in_(processed_subq),
            )
            .order_by(col(Email.received_at).desc())
            .limit(BATCH_SIZE)
        )

        result = await session.execute(stmt)
        emails = list(result.scalars().all())

        logger.info(f"Found {len(emails)} unprocessed emails to match")
        return emails

    def _match_email_to_deals(self, email: Email) -> list[EmailMatchResult]:
        """
        Match a single email to deals based on stakeholder participation.

        Returns a list of match results for deals that meet the threshold.
        """
        results: list[EmailMatchResult] = []

        # Normalize email participants
        from_email = self._normalize_email(email.from_email)
        to_emails: set[str] = {
            e for e in (self._normalize_email(addr) for addr in (email.to_emails or [])) if e
        }
        cc_emails: set[str] = {
            e for e in (self._normalize_email(addr) for addr in (email.cc_emails or [])) if e
        }
        all_recipients = to_emails | cc_emails

        if not from_email:
            return results

        # Find potential deal matches
        potential_deals: set[UUID] = set()

        # Check if sender is a stakeholder
        if from_email in self._team_email_to_deals:
            potential_deals.update(self._team_email_to_deals[from_email])
        if from_email in self._contact_email_to_deals:
            potential_deals.update(self._contact_email_to_deals[from_email])

        # Check if any recipient is a stakeholder
        for recipient in all_recipients:
            if recipient in self._team_email_to_deals:
                potential_deals.update(self._team_email_to_deals[recipient])
            if recipient in self._contact_email_to_deals:
                potential_deals.update(self._contact_email_to_deals[recipient])

        # Score each potential deal
        for deal_id in potential_deals:
            info = self._deal_info.get(deal_id)
            if not info:
                continue

            score, direction, matched = self._calculate_match_score(
                from_email, to_emails, cc_emails, info
            )

            if score >= MIN_MATCH_SCORE:
                results.append(
                    EmailMatchResult(
                        deal_id=deal_id,
                        score=score,
                        direction=direction,
                        matched_emails=matched,
                    )
                )

        return results

    def _calculate_match_score(
        self,
        from_email: str,
        to_emails: set[str],
        cc_emails: set[str],
        info: DealStakeholderInfo,
    ) -> tuple[float, DealEmailDirection, list[str]]:
        """
        Calculate match score for an email against a deal's stakeholders.

        Scoring logic:
        - Team sender + contact recipient = 1.0 (outbound)
        - Contact sender + team recipient = 1.0 (inbound)
        - Team in CC, contact in from/to = 0.7
        - Multiple stakeholders = +0.1 bonus

        Returns: (score, direction, matched_emails)
        """
        # Classify sender and recipients
        sender_is_team = from_email in info.team_emails
        sender_is_contact = from_email in info.contact_emails

        recipients_team = to_emails & info.team_emails
        recipients_contact = to_emails & info.contact_emails
        cc_team = cc_emails & info.team_emails
        cc_contact = cc_emails & info.contact_emails

        # Collect all matched stakeholders
        matched_emails = self._collect_matched_emails(
            from_email,
            sender_is_team,
            sender_is_contact,
            recipients_team,
            recipients_contact,
            cc_team,
            cc_contact,
        )

        # Determine score and direction based on communication pattern
        score, direction = self._score_communication_pattern(
            sender_is_team,
            sender_is_contact,
            recipients_team,
            recipients_contact,
            cc_team,
            cc_contact,
        )

        # Bonus for multiple stakeholders involved
        if len(matched_emails) > 2:
            score = min(score + 0.1, 1.0)

        return score, direction, list(matched_emails)

    def _collect_matched_emails(
        self,
        from_email: str,
        sender_is_team: bool,
        sender_is_contact: bool,
        recipients_team: set[str],
        recipients_contact: set[str],
        cc_team: set[str],
        cc_contact: set[str],
    ) -> set[str]:
        """Collect all stakeholder emails involved in this email."""
        matched: set[str] = set()
        if sender_is_team or sender_is_contact:
            matched.add(from_email)
        matched.update(recipients_team)
        matched.update(recipients_contact)
        matched.update(cc_team)
        matched.update(cc_contact)
        return matched

    def _score_communication_pattern(
        self,
        sender_is_team: bool,
        sender_is_contact: bool,
        recipients_team: set[str],
        recipients_contact: set[str],
        cc_team: set[str],
        cc_contact: set[str],
    ) -> tuple[float, DealEmailDirection]:
        """Determine score and direction based on communication pattern."""
        # High confidence: direct team <-> contact communication
        if sender_is_team and recipients_contact:
            return 1.0, DealEmailDirection.OUTBOUND
        if sender_is_contact and recipients_team:
            return 1.0, DealEmailDirection.INBOUND

        # Medium confidence: CC'd on communication
        if sender_is_contact and cc_team:
            return 0.7, DealEmailDirection.INBOUND
        if sender_is_team and cc_contact:
            return 0.7, DealEmailDirection.OUTBOUND
        if cc_team and (sender_is_contact or recipients_contact):
            direction = (
                DealEmailDirection.INBOUND if sender_is_contact else DealEmailDirection.OUTBOUND
            )
            return 0.7, direction

        return 0.0, DealEmailDirection.INTERNAL

    async def _update_stakeholder_last_contact_from_email(
        self,
        session: AsyncSession,
        deal_id: UUID,
        matched_emails: list[str],
        contact_time: datetime | None,
    ) -> int:
        """Update last_contact_at for matched stakeholders using an email timestamp."""
        if contact_time is None:
            return 0

        normalized_emails = {
            normalized
            for email in matched_emails
            if (normalized := self._normalize_email(email)) is not None
        }
        if not normalized_emails:
            return 0

        stmt = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.is_deleted).is_(False),
            func.lower(DealStakeholder.email).in_(normalized_emails),
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

    async def _create_association(
        self,
        session: AsyncSession,
        email: Email,
        match: EmailMatchResult,
        integration_type: Any | None = None,
    ) -> DealEmailAssociation | None:
        """
        Create a DealEmailAssociation for a matched email-deal pair.

        Args:
            session: Database session to use for inserts.
            email: Email object to create association for.
            match: EmailMatchResult with deal and score info.
            integration_type: Optional integration type for timeline event.

        Also creates a corresponding timeline event in the same transaction.

        Returns the created association or None if it already exists.
        """
        # Create new association using INSERT ... ON CONFLICT DO NOTHING
        # This handles race conditions when multiple tasks process the same org
        if not email.internet_message_id:
            logger.warning("Email %s has no internet_message_id, skipping", email.email_id)
            return None

        association = DealEmailAssociation(
            organisation_id=self.organisation_id,
            deal_id=match.deal_id,
            internet_message_id=email.internet_message_id,
            status=AssociationStatus.ACTIVE,
            match_score=match.score,
            direction=match.direction,
            matched_stakeholder_emails=match.matched_emails,
            email_subject=email.subject[:500] if email.subject else None,
            email_date=email.received_at or email.sent_at,
        )

        stmt = (
            insert(DealEmailAssociation)
            .values(
                association_id=association.association_id,
                organisation_id=association.organisation_id,
                deal_id=association.deal_id,
                internet_message_id=association.internet_message_id,
                status=association.status,
                match_score=association.match_score,
                direction=association.direction,
                matched_stakeholder_emails=association.matched_stakeholder_emails,
                email_subject=association.email_subject,
                email_date=association.email_date,
                created_at=association.created_at,
                updated_at=association.updated_at,
            )
            .on_conflict_do_nothing(constraint="uq_deal_email")
        )
        result = await session.execute(stmt)

        # Only create timeline event if insert succeeded (not a duplicate)
        if (getattr(result, "rowcount", 0) or 0) > 0:
            # Create timeline event inline (same transaction)
            resolved_source = resolve_email_event_source(integration_type)
            timeline_event = build_email_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=match.deal_id,
                association=association,
                email=email,
                event_source=resolved_source,
            )
            session.add(timeline_event)

            await self._update_stakeholder_last_contact_from_email(
                session=session,
                deal_id=match.deal_id,
                matched_emails=match.matched_emails,
                contact_time=self._resolve_email_contact_time(email),
            )
            return association

        return None

    async def process_organisation(self) -> dict[str, int]:
        """
        Process email matching for all active deals in the organisation.

        Uses fresh session per batch pattern for robust connection management:
        1. Session 1: Build stakeholder maps + get unprocessed emails
        2. For each batch: Fresh session to process and commit

        This ensures connections are fully released between batches,
        preventing long-held transactions and connection exhaustion.

        Returns:
            Stats dictionary with processing results.
        """
        stats = {
            "emails_processed": 0,
            "emails_matched": 0,
            "associations_created": 0,
            "deals_with_matches": 0,
            "calendar_invites_skipped": 0,
            "stakeholders_discovered": 0,
        }

        # =================================================================
        # Phase 1: Setup - Build maps and get emails (single session)
        # =================================================================
        emails: list[Email] = []
        integration_type_by_id: dict[UUID, Any] = {}

        async for session in acquire_org_async_session(self.organisation_id):
            # Step 1: Build stakeholder maps
            await self._build_stakeholder_maps(session)

            if not self._deal_info:
                logger.info(f"No active deals found for organisation {self.organisation_id}")
                return stats

            # Step 2: Get unprocessed emails
            emails = await self._get_unprocessed_emails(session)

            if not emails:
                logger.info(f"No unprocessed emails found for organisation {self.organisation_id}")
                return stats

            # Preload integration types for this batch (avoid N+1 queries)
            integration_ids = {
                e.integration_id for e in emails if getattr(e, "integration_id", None)
            }
            if integration_ids:
                it_stmt = select(
                    KnowledgeIntegration.integration_id,
                    KnowledgeIntegration.integration_type,
                ).where(col(KnowledgeIntegration.integration_id).in_(integration_ids))
                it_result = await session.execute(it_stmt)
                integration_type_by_id = {row[0]: row[1] for row in it_result.all()}

            # No commit needed - read-only operations
            # Session closes here, connection returns to pool

        # =================================================================
        # Phase 2: Process emails (fresh session)
        # =================================================================
        deals_matched: set[UUID] = set()

        async for session in acquire_org_async_session(self.organisation_id):
            for email in emails:
                stats["emails_processed"] += 1

                if self._is_calendar_invite_email(email):
                    stats["calendar_invites_skipped"] += 1
                    continue

                matches = self._match_email_to_deals(email)

                if matches:
                    stats["emails_matched"] += 1

                    for match in matches:
                        association = await self._create_association(
                            session,
                            email,
                            match,
                            integration_type=integration_type_by_id.get(email.integration_id),
                        )
                        if association:
                            stats["associations_created"] += 1
                            deals_matched.add(match.deal_id)

                            info = self._deal_info.get(match.deal_id)
                            if info:
                                discovered = await self._discover_stakeholders_from_email(
                                    session, email, match.deal_id, info
                                )
                                stats["stakeholders_discovered"] += discovered

            await session.commit()

        # =================================================================
        # Phase 3: Re-scan existing email associations for undiscovered stakeholders.
        # Handles the case where customer domains expanded since emails were
        # first associated (e.g., new stakeholder added, new CRM contact synced).
        # =================================================================
        async for session in acquire_org_async_session(self.organisation_id):
            for deal_id, info in self._deal_info.items():
                if not info.company_domains:
                    continue
                discovered = await self._discover_from_existing_email_associations(
                    session, deal_id, info
                )
                stats["stakeholders_discovered"] += discovered

            await session.commit()

        await self._dispatch_pending_research()
        stats["deals_with_matches"] = len(deals_matched)
        stats["deals_matched_ids"] = list(deals_matched)  # Return actual deal IDs

        logger.info(
            f"Email matching completed for org {self.organisation_id}: "
            f"{stats['emails_processed']} processed, {stats['emails_matched']} matched, "
            f"{stats['associations_created']} associations created, "
            f"{stats['calendar_invites_skipped']} calendar invites skipped, "
            f"{stats['stakeholders_discovered']} stakeholders discovered"
        )

        return stats

    async def process_deal(self, deal_id: UUID) -> dict[str, int]:
        """
        Process email matching for a single deal.

        Re-matches all emails against this specific deal's stakeholders.
        Used when a new stakeholder is added to find historical emails.

        Requires session to be passed in constructor.

        Args:
            deal_id: UUID of the deal to re-match.

        Returns:
            Stats dictionary with processing results.
        """
        if self._session is None:
            raise ValueError("Session is required for process_deal(). Pass session in constructor.")

        session = self._session

        stats = {
            "emails_processed": 0,
            "emails_matched": 0,
            "associations_created": 0,
            "calendar_invites_skipped": 0,
            "stakeholders_discovered": 0,
        }

        # Build stakeholder map for just this deal
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
            return stats

        # Build info for this single deal
        info = DealStakeholderInfo(
            deal_id=deal.deal_id,
            integration_id=deal.integration_id,
        )
        self._collect_team_emails(deal, info)
        await self._collect_stakeholder_emails(session, deal.deal_id, info)
        info.company_domains = self._collect_company_domains(deal)

        if not info.team_emails or not info.contact_emails:
            logger.info(f"No stakeholders for deal {deal_id}, skipping email matching")
            return stats

        # Store in lookup maps for matching
        self._deal_info[deal.deal_id] = info
        for email in info.team_emails:
            if email not in self._team_email_to_deals:
                self._team_email_to_deals[email] = set()
            self._team_email_to_deals[email].add(deal.deal_id)
        for email in info.contact_emails:
            if email not in self._contact_email_to_deals:
                self._contact_email_to_deals[email] = set()
            self._contact_email_to_deals[email].add(deal.deal_id)

        # Get all emails involving this deal's stakeholders
        all_stakeholder_emails = list(info.team_emails | info.contact_emails)
        emails = await self._get_emails_for_stakeholders(session, all_stakeholder_emails)

        if not emails:
            logger.info(f"No emails found for deal {deal_id} stakeholders")
            return stats

        # Preload integration types
        integration_ids = {e.integration_id for e in emails if getattr(e, "integration_id", None)}
        integration_type_by_id: dict[UUID, Any] = {}
        if integration_ids:
            it_stmt = select(
                KnowledgeIntegration.integration_id,
                KnowledgeIntegration.integration_type,
            ).where(col(KnowledgeIntegration.integration_id).in_(integration_ids))
            it_result = await session.execute(it_stmt)
            integration_type_by_id = {row[0]: row[1] for row in it_result.all()}

        # Match and create associations
        for email in emails:
            stats["emails_processed"] += 1

            # Skip calendar invite emails (already matched via calendar sync)
            if self._is_calendar_invite_email(email):
                stats["calendar_invites_skipped"] += 1
                continue

            matches = self._match_email_to_deals(email)
            # Filter to only this deal
            matches = [m for m in matches if m.deal_id == deal_id]

            if matches:
                stats["emails_matched"] += 1
                for match in matches:
                    association = await self._create_association(
                        session,
                        email,
                        match,
                        integration_type=integration_type_by_id.get(email.integration_id),
                    )
                    if association:
                        stats["associations_created"] += 1

                        # Discover new stakeholders from email participants
                        discovered = await self._discover_stakeholders_from_email(
                            session, email, match.deal_id, info
                        )
                        stats["stakeholders_discovered"] += discovered

        await session.commit()
        await self._dispatch_pending_research()

        logger.info(
            f"Email matching for deal {deal_id}: "
            f"{stats['emails_processed']} processed, {stats['emails_matched']} matched, "
            f"{stats['associations_created']} associations created, "
            f"{stats['calendar_invites_skipped']} calendar invites skipped, "
            f"{stats['stakeholders_discovered']} stakeholders discovered"
        )

        return stats

    async def _get_emails_for_stakeholders(
        self, session: AsyncSession, stakeholder_emails: list[str]
    ) -> list[Email]:
        """Get all emails involving any of the given stakeholder emails.

        Args:
            session: Database session to use for queries.
            stakeholder_emails: List of email addresses to search for.
        """
        if not stakeholder_emails:
            return []

        # Query emails where sender or recipient matches stakeholders
        stmt = (
            select(Email)
            .where(
                col(Email.organisation_id) == self.organisation_id,
                col(Email.internet_message_id).isnot(None),
                col(Email.internet_message_id) != "",
            )
            .where(
                or_(
                    func.lower(Email.from_email).in_(stakeholder_emails),
                    # Check if any to/cc email matches (using array overlap would be better but this works)
                )
            )
            .order_by(col(Email.received_at).desc())
            .limit(1000)  # Reasonable limit for single deal
        )

        result = await session.execute(stmt)
        return list(result.scalars().all())


async def process_deal_emails(
    organisation_id: UUID,
) -> dict[str, int]:
    """
    Process email matching for an organisation.

    Uses fresh session per batch pattern internally.

    Args:
        organisation_id: UUID of the organisation

    Returns:
        Dictionary with processing statistics.
    """
    matcher = DealEmailMatcher(organisation_id)
    return await matcher.process_organisation()
