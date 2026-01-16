"""Stakeholder sync service for Task 1.

This service syncs stakeholders from CRM deal participants (CRMDealParticipant)
to the DealStakeholder table.

DATA SOURCE:
------------
All stakeholder data comes from CRMDealParticipant table ONLY.
We do NOT use CRMDeal.primary_contact_email - that field is for future use.

CRMDealParticipant contains:
- participant_type: CONTACT, COMPANY, USER
- related_email: Contact's email address
- related_name: Contact's display name
- relationship_role: Role in deal (e.g., "decision_maker", "technical_buyer")
- is_primary: Whether this is the primary contact

Only CONTACT type participants become stakeholders.
USER type = internal team members (not stakeholders).
COMPANY type = organization entities (not individual stakeholders).

Flow:
-----
    CRM Sync → commits CRMDealParticipant records
           ↓
    Stakeholder Task → calls this service
           ↓
    This service → reads CRMDealParticipant (type=CONTACT)
                → creates/updates DealStakeholder records
                → returns result with requires_rematching flag
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMContact, CRMDeal
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)
from src.infra.utils.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


@dataclass
class StakeholderSyncResult:
    """Result of syncing stakeholders for a deal."""

    deal_id: UUID
    integration_id: UUID | None = None
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    @property
    def requires_rematching(self) -> bool:
        """True if new stakeholders were created (need calendar/email matching)."""
        return self.created > 0


@dataclass
class OrgStakeholderSyncResult:
    """Result of syncing stakeholders for an entire organization."""

    organisation_id: UUID
    total_deals: int = 0
    successful_deals: int = 0
    failed_deals: int = 0
    stakeholders_created: int = 0
    stakeholders_updated: int = 0
    stakeholders_deactivated: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.failed_deals == 0

    @property
    def requires_rematching(self) -> bool:
        """True if any stakeholders were created (need calendar/email matching)."""
        return self.stakeholders_created > 0


class StakeholderSyncService:
    """Syncs stakeholders from CRM deal participants.

    Usage (per-deal):
        service = StakeholderSyncService(session, organisation_id)
        result = await service.sync_for_deal(deal_id)
        if result.requires_rematching:
            # Dispatch Task 2/3

    Usage (org-level - for initial sync):
        service = StakeholderSyncService(session, organisation_id)
        result = await service.sync_for_organisation()
        if result.requires_rematching:
            # Dispatch Task 2/3 for entire org
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def sync_for_organisation(self) -> OrgStakeholderSyncResult:
        """Sync stakeholders for ALL deals in the organization.

        This is used after CRM initial sync to ensure all deals have their
        stakeholders synced before calendar/email matching runs.

        Returns:
            OrgStakeholderSyncResult with aggregate counts
        """
        result = OrgStakeholderSyncResult(organisation_id=self.organisation_id)

        # Get all deal IDs for this org
        stmt = select(CRMDeal.deal_id).where(  # type: ignore
            col(CRMDeal.organisation_id) == self.organisation_id
        )
        query_result = await self.session.execute(stmt)
        deal_ids = [row[0] for row in query_result.all()]
        result.total_deals = len(deal_ids)

        if result.total_deals == 0:
            logger.info("No deals found for org %s", self.organisation_id)
            return result

        logger.info(
            "Syncing stakeholders for %d deals in org %s",
            result.total_deals,
            self.organisation_id,
        )

        # Sync stakeholders for each deal
        for deal_id in deal_ids:
            try:
                deal_result = await self.sync_for_deal(deal_id)
                result.successful_deals += 1
                result.stakeholders_created += deal_result.created
                result.stakeholders_updated += deal_result.updated
                result.stakeholders_deactivated += deal_result.deactivated

                if deal_result.errors:
                    result.errors.extend(deal_result.errors[:3])  # Limit per deal

            except Exception as e:
                # CRITICAL: Rollback to clear the failed transaction state
                # Otherwise all subsequent queries will fail with:
                # "current transaction is aborted, commands ignored until end of transaction block"
                await self.session.rollback()

                result.failed_deals += 1
                result.errors.append(f"Deal {deal_id}: {str(e)}")
                logger.warning(
                    "Failed to sync stakeholders for deal %s: %s",
                    deal_id,
                    str(e),
                )

        logger.info(
            "Org stakeholder sync complete: org=%s deals=%d/%d created=%d updated=%d",
            self.organisation_id,
            result.successful_deals,
            result.total_deals,
            result.stakeholders_created,
            result.stakeholders_updated,
        )

        return result

    async def sync_for_deal(self, deal_id: UUID) -> StakeholderSyncResult:
        """Sync stakeholders for a deal from CRMDealParticipant records.

        Only processes participants where participant_type = CONTACT.

        Args:
            deal_id: UUID of the deal to sync

        Returns:
            StakeholderSyncResult with counts and requires_rematching flag
        """
        # Load deal with participants
        deal = await self._load_deal(deal_id)
        if not deal:
            return StakeholderSyncResult(
                deal_id=deal_id,
                errors=[f"Deal {deal_id} not found"],
            )

        result = StakeholderSyncResult(
            deal_id=deal_id,
            integration_id=deal.integration_id,
        )

        # Load existing stakeholders
        existing_stakeholders = await self._load_stakeholders(deal_id)
        existing_by_email: dict[str, DealStakeholder] = {
            s.email.lower(): s for s in existing_stakeholders if s.email
        }

        # Track emails we see in current participants
        seen_emails: set[str] = set()

        # Process only CONTACT type participants
        for participant in deal.participants or []:
            # Skip non-CONTACT types
            if participant.participant_type != CRMParticipantType.CONTACT:
                continue

            # Skip participants with no email
            if not participant.related_email:
                result.skipped += 1
                logger.debug(
                    "Skipped participant %s - no email",
                    participant.participant_id,
                )
                continue

            email = participant.related_email.lower()
            seen_emails.add(email)

            was_created = await self._ensure_stakeholder(
                deal=deal,
                participant=participant,
                existing=existing_by_email.get(email),
            )

            if was_created:
                result.created += 1
            else:
                result.updated += 1

        # Soft-delete stakeholders no longer in CRM participants
        for email, stakeholder in existing_by_email.items():
            should_deactivate = (
                email not in seen_emails
                and stakeholder.source == StakeholderSource.CRM
                and not stakeholder.is_deleted
            )
            if should_deactivate:
                stakeholder.is_deleted = True
                stakeholder.updated_at = datetime.now(UTC)
                self.session.add(stakeholder)
                result.deactivated += 1

        await self.session.commit()

        logger.info(
            "Stakeholder sync for deal %s: created=%d updated=%d deactivated=%d skipped=%d",
            deal_id,
            result.created,
            result.updated,
            result.deactivated,
            result.skipped,
        )

        return result

    async def _load_deal(self, deal_id: UUID) -> CRMDeal | None:
        """Load deal with participants."""
        stmt = (
            select(CRMDeal)
            .where(col(CRMDeal.deal_id) == deal_id)
            .options(selectinload(CRMDeal.participants))  # type: ignore
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _load_stakeholders(self, deal_id: UUID) -> list[DealStakeholder]:
        """Load existing stakeholders for a deal."""
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _ensure_stakeholder(
        self,
        deal: CRMDeal,
        participant: CRMDealParticipant,
        existing: DealStakeholder | None = None,
    ) -> bool:
        """Create or update a stakeholder from a CRMDealParticipant.

        Args:
            deal: The CRMDeal record
            participant: The CRMDealParticipant record (must be type=CONTACT)
            existing: Existing DealStakeholder if found by email

        Returns:
            True if created new stakeholder, False if updated existing.
            Returns False without changes if stakeholder was REMOVED by user.
        """
        email = participant.related_email.lower()  # type: ignore

        if existing:
            # Respect user decision: if stakeholder was REMOVED, don't reactivate
            if existing.approval_status == ApprovalStatus.REMOVED:
                return False

            # TODO: Currently updates fields unconditionally. Future improvement:
            # Compare old vs new values before updating to avoid unnecessary DB writes
            # and misleading updated_at timestamps. Should only update if values differ.
            existing.source = StakeholderSource.CRM
            existing.source_reference_id = participant.participant_id
            existing.approval_status = ApprovalStatus.APPROVED  # CRM is always trusted
            existing.is_deleted = False  # Reactivate if was soft-deleted
            existing.updated_at = datetime.now(UTC)

            # Update name if we have one and existing doesn't
            if participant.related_name and not existing.name:
                existing.name = participant.related_name

            # NOTE: Do NOT set job_title from participant.relationship_role
            # relationship_role contains HubSpot association type (e.g., "deal_to_contact")
            # job_title should come from CRMContact.job_title via enrichment

            # Enrich from CRMContact (fixes bad job_title values from old sync)
            await self._enrich_from_contact(existing, email)

            self.session.add(existing)
            return False

        # Create new stakeholder
        # NOTE: job_title is NOT set from participant.relationship_role
        # relationship_role contains HubSpot association type (e.g., "deal_to_contact")
        # job_title will be enriched from CRMContact.job_title below
        stakeholder = DealStakeholder(
            organisation_id=self.organisation_id,
            integration_id=deal.integration_id,
            deal_id=deal.deal_id,
            email=email,
            name=participant.related_name,
            source=StakeholderSource.CRM,
            source_reference_id=participant.participant_id,
            approval_status=ApprovalStatus.APPROVED,  # CRM is always trusted
            job_title=None,  # Will be enriched from CRMContact
            stance=StakeholderStance.UNKNOWN,
            temperature=StakeholderTemperature.UNKNOWN,
            engagement_score=0.0,
            last_contact_at=None,
        )

        # Enrich from CRMContact if available
        await self._enrich_from_contact(stakeholder, email)

        self.session.add(stakeholder)
        return True

    async def _enrich_from_contact(self, stakeholder: DealStakeholder, email: str) -> None:
        """Enrich stakeholder with CRMContact data if available.

        CRMContact is the authoritative source for job_title, so we always
        overwrite (fixes bad values from old sync that used relationship_role).
        """
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.email) == email,
        )
        result = await self.session.execute(stmt)
        contact = result.scalars().first()

        if contact:
            stakeholder.contact_id = contact.contact_id
            if contact.full_name and not stakeholder.name:
                stakeholder.name = contact.full_name
            # Always use CRMContact.job_title as authoritative source
            if contact.job_title:
                stakeholder.job_title = contact.job_title
