"""Stakeholder Lookup Service for calendar and email matching.

This service builds and provides stakeholder lookup maps used by:
- Task 2: Calendar Matching (DealMeetingMatcher)
- Task 3: Email Matching (DealEmailMatcher)

The service provides in-memory lookup maps that enable efficient matching:
- team_email → [deal_ids] - Find deals where a team member is involved
- contact_email → [deal_ids] - Find deals where a contact is involved
- company_domain → [deal_ids] - Find deals by company domain

ARCHITECTURE:
-------------
    DealMeetingMatcher ──┐
                         │
                         ├──> StakeholderLookupService (this file)
                         │
    DealEmailMatcher ────┘

This shared service eliminates duplicate map-building logic in both matchers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
)
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


@dataclass
class DealLookupInfo:
    """Container for a single deal's stakeholder information.

    Used for matching calendar events and emails to deals.
    """

    deal_id: UUID
    integration_id: UUID | None = None
    team_emails: set[str] = field(default_factory=set)
    contact_emails: set[str] = field(default_factory=set)
    company_domains: set[str] = field(default_factory=set)

    @property
    def all_stakeholder_emails(self) -> set[str]:
        """All emails that can be used for matching (team + contact)."""
        return self.team_emails | self.contact_emails


@dataclass
class StakeholderLookupMaps:
    """In-memory lookup maps for efficient email-to-deal matching.

    These maps enable O(1) lookup of deals by stakeholder email or domain.
    """

    team_email_to_deals: dict[str, set[UUID]] = field(default_factory=dict)
    contact_email_to_deals: dict[str, set[UUID]] = field(default_factory=dict)
    domain_to_deals: dict[str, set[UUID]] = field(default_factory=dict)
    deal_info: dict[UUID, DealLookupInfo] = field(default_factory=dict)

    @property
    def total_deals(self) -> int:
        """Number of deals in the lookup maps."""
        return len(self.deal_info)

    @property
    def total_team_emails(self) -> int:
        """Number of unique team emails."""
        return len(self.team_email_to_deals)

    @property
    def total_contact_emails(self) -> int:
        """Number of unique contact emails."""
        return len(self.contact_email_to_deals)

    def get_deals_for_email(self, email: str) -> set[UUID]:
        """Get all deal IDs where this email is a stakeholder (team or contact)."""
        email = email.lower()
        deals = self.team_email_to_deals.get(email, set())
        deals = deals | self.contact_email_to_deals.get(email, set())
        return deals

    def get_deals_for_domain(self, domain: str) -> set[UUID]:
        """Get all deal IDs associated with this company domain."""
        return self.domain_to_deals.get(domain.lower(), set())

    def is_team_email(self, email: str) -> bool:
        """Check if email belongs to a team member."""
        return email.lower() in self.team_email_to_deals

    def is_contact_email(self, email: str) -> bool:
        """Check if email belongs to a known contact."""
        return email.lower() in self.contact_email_to_deals


class StakeholderLookupService:
    """Service for building and querying stakeholder lookup maps.

    Used by calendar and email matchers to find deals for a given email.

    Usage:
        service = StakeholderLookupService(session, organisation_id)
        maps = await service.build_lookup_maps()

        # Find deals where alice@acme.com is involved
        deals = maps.get_deals_for_email("alice@acme.com")

        # Check if email is team or contact
        is_internal = maps.is_team_email("bob@mycompany.com")
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        """Initialize the service.

        Args:
            session: Async database session for the organisation database
            organisation_id: UUID of the organisation
        """
        self.session = session
        self.organisation_id = organisation_id

    async def build_lookup_maps(
        self,
        *,
        deal_status: CRMDealStatus = CRMDealStatus.OPEN,
        include_closed: bool = False,
    ) -> StakeholderLookupMaps:
        """Build lookup maps for all deals in the organisation.

        Args:
            deal_status: Filter deals by status (default: OPEN)
            include_closed: If True, include closed deals as well

        Returns:
            StakeholderLookupMaps with populated lookup dictionaries
        """
        maps = StakeholderLookupMaps()

        # Fetch all deals with participants
        deal_participants_rel = cast(Any, CRMDeal.participants)

        stmt = (
            select(CRMDeal)
            .options(selectinload(deal_participants_rel))
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.is_deleted).is_(False),
            )
        )

        if not include_closed:
            stmt = stmt.where(col(CRMDeal.deal_status) == deal_status)

        result = await self.session.execute(stmt)
        deals = result.scalars().all()

        logger.info(
            "Building stakeholder lookup maps for %d deals (org=%s)",
            len(deals),
            self.organisation_id,
            extra={
                "event": "stakeholder_lookup_maps_build_started",
                "organisation_id": str(self.organisation_id),
                "deals": len(deals),
            },
        )

        # Build maps for each deal
        for deal in deals:
            await self._build_deal_lookup(deal, maps)

        logger.info(
            "Stakeholder maps built: deals=%d team_emails=%d contact_emails=%d domains=%d",
            maps.total_deals,
            maps.total_team_emails,
            maps.total_contact_emails,
            len(maps.domain_to_deals),
            extra={
                "event": "stakeholder_lookup_maps_build_completed",
                "organisation_id": str(self.organisation_id),
                "deals": maps.total_deals,
                "team_emails": maps.total_team_emails,
                "contact_emails": maps.total_contact_emails,
                "domains": len(maps.domain_to_deals),
            },
        )

        return maps

    async def build_lookup_for_deal(self, deal_id: UUID) -> DealLookupInfo | None:
        """Build lookup info for a single deal.

        Args:
            deal_id: UUID of the deal

        Returns:
            DealLookupInfo for the deal, or None if not found
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
            return None

        info = DealLookupInfo(
            deal_id=deal.deal_id,
            integration_id=deal.integration_id,
        )

        # Collect team and contact emails
        self._collect_team_emails(deal, info)
        await self._collect_contact_emails(deal, info)
        self._collect_company_domains(deal, info)

        return info

    async def get_all_team_emails(self) -> set[str]:
        """Get all team member emails across all deals in the organisation.

        This is useful for filtering out internal emails in matching logic.

        Returns:
            Set of normalized team member email addresses
        """
        maps = await self.build_lookup_maps()
        return set(maps.team_email_to_deals.keys())

    # =========================================================================
    # PRIVATE METHODS
    # =========================================================================

    async def _build_deal_lookup(
        self,
        deal: CRMDeal,
        maps: StakeholderLookupMaps,
    ) -> None:
        """Build lookup info for a single deal and add to maps."""
        info = DealLookupInfo(
            deal_id=deal.deal_id,
            integration_id=deal.integration_id,
        )

        # Collect team emails (USER participants)
        self._collect_team_emails(deal, info)

        # Collect contact emails (CONTACT participants + stakeholders)
        await self._collect_contact_emails(deal, info)

        # Collect company domains
        self._collect_company_domains(deal, info)

        # Store deal info
        maps.deal_info[deal.deal_id] = info

        # Build reverse lookup maps
        for email in info.team_emails:
            if email not in maps.team_email_to_deals:
                maps.team_email_to_deals[email] = set()
            maps.team_email_to_deals[email].add(deal.deal_id)

        for email in info.contact_emails:
            if email not in maps.contact_email_to_deals:
                maps.contact_email_to_deals[email] = set()
            maps.contact_email_to_deals[email].add(deal.deal_id)

        for domain in info.company_domains:
            if domain not in maps.domain_to_deals:
                maps.domain_to_deals[domain] = set()
            maps.domain_to_deals[domain].add(deal.deal_id)

    def _collect_team_emails(self, deal: CRMDeal, info: DealLookupInfo) -> None:
        """Collect team member emails from deal.

        Team emails come from:
        1. CRMDeal.owner_email - Primary deal owner
        2. CRMDealParticipant where participant_type = USER
           - Includes: owner, collaborator, split_owner, technical_owner roles
        """
        # Deal owner email
        owner_email = self._normalize_email(deal.owner_email)
        if owner_email:
            info.team_emails.add(owner_email)

        # All USER participants (collaborators, split_owners, etc.)
        participants = self._get_participants(deal)
        for participant in participants:
            if participant.participant_type != CRMParticipantType.USER:
                continue
            email = self._normalize_email(participant.related_email)
            if email:
                info.team_emails.add(email)

    async def _collect_contact_emails(self, deal: CRMDeal, info: DealLookupInfo) -> None:
        """Collect contact emails from deal participants and stakeholders.

        Contact emails come from:
        1. CRMDeal.primary_contact_email
        2. CRMDealParticipant where participant_type = CONTACT
        3. DealStakeholder table (discovered stakeholders)
        """
        # Primary contact email
        primary_email = self._normalize_email(deal.primary_contact_email)
        if primary_email:
            info.contact_emails.add(primary_email)

        # CONTACT participants
        participants = self._get_participants(deal)
        for participant in participants:
            if participant.participant_type != CRMParticipantType.CONTACT:
                continue
            email = self._normalize_email(participant.related_email)
            if email and email not in info.team_emails:
                info.contact_emails.add(email)

        # Stakeholders from DealStakeholder table (only APPROVED for matching)
        stmt = select(col(DealStakeholder.email)).where(
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.deal_id) == deal.deal_id,
            col(DealStakeholder.is_deleted).is_(False),
            col(DealStakeholder.approval_status) == ApprovalStatus.APPROVED,
        )
        result = await self.session.execute(stmt)
        stakeholder_emails = result.scalars().all()

        for email in stakeholder_emails:
            normalized = self._normalize_email(email)
            if normalized and normalized not in info.team_emails:
                info.contact_emails.add(normalized)

    def _collect_company_domains(self, deal: CRMDeal, info: DealLookupInfo) -> None:
        """Collect company domains from deal.

        Company domains come from:
        1. Primary contact email domain
        2. CONTACT participant email domains
        3. COMPANY participant domains (if available)
        """
        # Extract domain from primary contact
        if deal.primary_contact_email:
            domain = self._extract_domain(deal.primary_contact_email)
            if domain:
                info.company_domains.add(domain)

        # Extract domains from CONTACT participants
        participants = self._get_participants(deal)
        for participant in participants:
            if (
                participant.participant_type == CRMParticipantType.CONTACT
                and participant.related_email
            ):
                domain = self._extract_domain(participant.related_email)
                if domain:
                    info.company_domains.add(domain)

            # COMPANY participants might have domain info
            # Companies might store domain in related_email or external reference
            if (
                participant.participant_type == CRMParticipantType.COMPANY
                and participant.related_email
            ):
                domain = self._extract_domain(participant.related_email)
                if domain:
                    info.company_domains.add(domain)

        # Also extract from contact emails already collected
        for email in info.contact_emails:
            domain = self._extract_domain(email)
            if domain:
                info.company_domains.add(domain)

    def _get_participants(self, deal: CRMDeal) -> list[CRMDealParticipant]:
        """Get participants list from deal, handling None case."""
        participants = cast(
            list[CRMDealParticipant] | None,
            getattr(deal, "participants", None),
        )
        return participants or []

    def _normalize_email(self, email: str | None) -> str | None:
        """Normalize email string by stripping whitespace and converting to lowercase."""
        if not email:
            return None
        normalized = email.strip().lower()
        if not normalized or normalized == "null":
            return None
        return normalized

    def _extract_domain(self, email: str) -> str | None:
        """Extract the domain part from an email address."""
        if not email or "@" not in email:
            return None
        return email.split("@")[1].lower()


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


async def build_org_stakeholder_maps(
    session: AsyncSession,
    organisation_id: UUID,
) -> StakeholderLookupMaps:
    """Build stakeholder lookup maps for an organisation.

    Args:
        session: Database session
        organisation_id: Organisation UUID

    Returns:
        StakeholderLookupMaps for efficient email-to-deal matching
    """
    service = StakeholderLookupService(session, organisation_id)
    return await service.build_lookup_maps()


async def get_deal_stakeholder_info(
    session: AsyncSession,
    organisation_id: UUID,
    deal_id: UUID,
) -> DealLookupInfo | None:
    """Get stakeholder info for a single deal.

    Args:
        session: Database session
        organisation_id: Organisation UUID
        deal_id: Deal UUID

    Returns:
        DealLookupInfo with team, contact emails and company domains
    """
    service = StakeholderLookupService(session, organisation_id)
    return await service.build_lookup_for_deal(deal_id)
