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
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col, select
from src.domain.models.organisation_tables.sync.calendar import AssociationStatus
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
)
from src.infra.utils.logger import get_logger
from src.service.crm.deal_timeline_sync_service import build_email_timeline_event

logger = get_logger(__name__)


# Configuration constants
MIN_MATCH_SCORE = 0.7
BATCH_SIZE = 500


@dataclass
class DealStakeholderInfo:
    """Container for deal stakeholder information."""

    deal_id: UUID
    team_emails: set[str] = field(default_factory=set)
    contact_emails: set[str] = field(default_factory=set)


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

        # In-memory lookup maps (populated by _build_stakeholder_maps)
        self._team_email_to_deals: dict[str, set[UUID]] = {}
        self._contact_email_to_deals: dict[str, set[UUID]] = {}
        self._deal_info: dict[UUID, DealStakeholderInfo] = {}

    def _normalize_email(self, email: str | None) -> str | None:
        """Normalize email string by stripping whitespace and converting to lowercase."""
        if not email:
            return None
        normalized = email.strip().lower()
        if not normalized or normalized == "null":
            return None
        return normalized

    async def _build_stakeholder_maps(self) -> None:
        """
        Load all active deals and build stakeholder-to-deal lookup maps.

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
        result = await self.session.execute(stmt)
        deals = result.scalars().all()

        logger.info(f"Building stakeholder maps for {len(deals)} active deals")

        for deal in deals:
            info = DealStakeholderInfo(deal_id=deal.deal_id)

            # Collect team emails (owner + technical owner from participants)
            self._collect_team_emails(deal, info)

            # Collect contact emails from DealStakeholder table
            await self._collect_stakeholder_emails(deal.deal_id, info)

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

    async def _collect_stakeholder_emails(self, deal_id: UUID, info: DealStakeholderInfo) -> None:
        """
        Collect contact emails from DealStakeholder table.

        These are external contacts discovered from calendar meetings.
        """
        stmt = select(col(DealStakeholder.email)).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.deal_id) == deal_id,
        )
        result = await self.session.execute(stmt)
        stakeholder_emails = result.scalars().all()

        for email in stakeholder_emails:
            normalized = self._normalize_email(email)
            if normalized and normalized not in info.team_emails:
                info.contact_emails.add(normalized)

    async def _get_unprocessed_emails(self) -> list[Email]:
        """
        Get emails that have not been processed for deal matching.

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

        result = await self.session.execute(stmt)
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

    async def _create_association(
        self,
        email: Email,
        match: EmailMatchResult,
    ) -> DealEmailAssociation | None:
        """
        Create a DealEmailAssociation for a matched email-deal pair.

        Also creates a corresponding timeline event in the same transaction.

        Returns the created association or None if it already exists.
        """
        # Check if association already exists (defensive)
        stmt = select(DealEmailAssociation).where(
            col(DealEmailAssociation.deal_id) == match.deal_id,
            col(DealEmailAssociation.internet_message_id) == email.internet_message_id,
        )
        result = await self.session.execute(stmt)
        existing = result.scalars().first()

        if existing:
            return None

        # Create new association
        association = DealEmailAssociation(
            organisation_id=self.organisation_id,
            deal_id=match.deal_id,
            internet_message_id=email.internet_message_id,
            status=AssociationStatus.SUGGESTED,
            match_score=match.score,
            direction=match.direction,
            matched_stakeholder_emails=match.matched_emails,
            email_subject=email.subject[:500] if email.subject else None,
            email_date=email.received_at or email.sent_at,
        )

        self.session.add(association)

        # Create timeline event inline (same transaction)
        timeline_event = build_email_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=match.deal_id,
            association=association,
            email=email,
        )
        self.session.add(timeline_event)

        return association

    async def process_organisation(self) -> dict[str, int]:
        """
        Process email matching for all active deals in the organisation.

        Uses Email-First batch approach:
        1. Build stakeholder maps from all active deals
        2. Query unprocessed emails
        3. Match each email to relevant deals
        4. Create associations for matches above threshold

        Returns:
            Stats dictionary with processing results.
        """
        stats = {
            "emails_processed": 0,
            "emails_matched": 0,
            "associations_created": 0,
            "deals_with_matches": 0,
        }

        # Step 1: Build stakeholder maps
        await self._build_stakeholder_maps()

        if not self._deal_info:
            logger.info(f"No active deals found for organisation {self.organisation_id}")
            return stats

        # Step 2: Get unprocessed emails
        emails = await self._get_unprocessed_emails()

        if not emails:
            logger.info(f"No unprocessed emails found for organisation {self.organisation_id}")
            return stats

        # Track which deals got matches
        deals_matched: set[UUID] = set()

        # Step 3 & 4: Match emails and create associations
        for email in emails:
            stats["emails_processed"] += 1

            matches = self._match_email_to_deals(email)

            if matches:
                stats["emails_matched"] += 1

                for match in matches:
                    association = await self._create_association(email, match)
                    if association:
                        stats["associations_created"] += 1
                        deals_matched.add(match.deal_id)

        stats["deals_with_matches"] = len(deals_matched)

        await self.session.commit()

        logger.info(
            f"Email matching completed for org {self.organisation_id}: "
            f"{stats['emails_processed']} processed, {stats['emails_matched']} matched, "
            f"{stats['associations_created']} associations created"
        )

        return stats


async def process_deal_emails(
    session: AsyncSession,
    organisation_id: UUID,
) -> dict[str, int]:
    """
    Process email matching for an organisation.

    Args:
        session: Async database session for the organisation
        organisation_id: UUID of the organisation

    Returns:
        Dictionary with processing statistics.
    """
    matcher = DealEmailMatcher(session, organisation_id)
    return await matcher.process_organisation()
