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
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.organisation_tables.sync.crm import CRMContact, CRMDeal
from src.domain.models.organisation_tables.sync.crm.deals import (
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
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
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
    rejected: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return True when every stakeholder sync step for the deal succeeded."""
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
    stakeholders_rejected: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return True when the organization sync completed without failed deals."""
        return self.failed_deals == 0

    @property
    def requires_rematching(self) -> bool:
        """True if any stakeholders were created (need calendar/email matching)."""
        return self.stakeholders_created > 0


class StakeholderSyncService:
    """Syncs stakeholders from CRM deal participants.

    Uses fresh session pattern for better connection management:
    - Org-level: Acquires fresh session per deal, releases between deals
    - Deal-level: Acquires fresh session for each deal sync

    Usage (org-level - for initial sync):
        service = StakeholderSyncService(organisation_id)
        result = await service.sync_for_organisation()
        if result.requires_rematching:
            # Dispatch Task 2/3 for entire org

    Usage (per-deal):
        service = StakeholderSyncService(organisation_id)
        result = await service.sync_for_deal(deal_id)
        if result.requires_rematching:
            # Dispatch Task 2/3
    """

    def __init__(
        self,
        organisation_id: UUID,
        *,
        suppress_research_dispatch: bool = False,
        org_sync_batch_size: int = 100,
    ) -> None:
        """
        Initialize the stakeholder sync service.

        Args:
            organisation_id: UUID of the organisation
            suppress_research_dispatch: When True, newly synced stakeholders
                are not sent to the automated research dispatcher.
        """
        self.organisation_id = organisation_id
        self._suppress_research_dispatch = suppress_research_dispatch
        self._org_sync_batch_size = max(1, org_sync_batch_size)
        self._internal_domains: set[str] | None = None  # Lazy-loaded cache
        self._pending_research: list[tuple[UUID, UUID]] = []  # (stakeholder_id, deal_id)

    @staticmethod
    def _get_manual_overrides(stakeholder: DealStakeholder) -> set[str]:
        """Return user-managed fields that sync must not overwrite."""
        data = stakeholder.stakeholder_data or {}
        manual_overrides = data.get("manual_overrides")
        if not isinstance(manual_overrides, list):
            return set()
        return {str(field) for field in manual_overrides}

    async def sync_for_organisation(self) -> OrgStakeholderSyncResult:
        """Sync stakeholders for ALL deals in the organization.

        Uses fresh session pattern: acquires a new session for each deal,
        releasing connections between deal processing.

        This is used after CRM initial sync to ensure all deals have their
        stakeholders synced before calendar/email matching runs.

        Returns:
            OrgStakeholderSyncResult with aggregate counts
        """
        result = OrgStakeholderSyncResult(organisation_id=self.organisation_id)

        # Phase 1: Get all active deal IDs (fresh session)
        deal_ids: list[UUID] = []
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(col(CRMDeal.deal_id)).where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.is_deleted).is_(False),
                col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
            )
            query_result = await session.execute(stmt)
            deal_ids = [row[0] for row in query_result.all()]

        result.total_deals = len(deal_ids)

        if result.total_deals == 0:
            logger.info(
                "No deals found for org %s",
                self.organisation_id,
                extra={
                    "event": "stakeholder_sync_no_deals",
                    "organisation_id": str(self.organisation_id),
                },
            )
            return result

        logger.info(
            "Syncing stakeholders for %d deals in org %s",
            result.total_deals,
            self.organisation_id,
            extra={
                "event": "stakeholder_sync_started",
                "organisation_id": str(self.organisation_id),
                "total_deals": result.total_deals,
            },
        )

        # Phase 2: Process deals in bounded transactions. Large onboarded orgs can
        # have thousands of deals; one session per deal creates avoidable pool churn.
        for batch in self._iter_batches(deal_ids):
            async for session in acquire_org_async_session(self.organisation_id):
                for deal_id in batch:
                    pending_research_start = len(self._pending_research)
                    try:
                        async with self._deal_savepoint(session):
                            deal_result = await self._sync_deal_internal(session, deal_id)

                        result.successful_deals += 1
                        result.stakeholders_created += deal_result.created
                        result.stakeholders_updated += deal_result.updated
                        result.stakeholders_deactivated += deal_result.deactivated
                        result.stakeholders_rejected += deal_result.rejected

                        if deal_result.errors:
                            result.errors.extend(deal_result.errors[:3])  # Limit per deal

                    except Exception as e:
                        del self._pending_research[pending_research_start:]
                        result.failed_deals += 1
                        result.errors.append(f"Deal {deal_id}: {str(e)}")
                        logger.warning(
                            "Failed to sync stakeholders for deal %s: %s",
                            deal_id,
                            str(e),
                            extra={
                                "event": "stakeholder_sync_failed_for_deal",
                                "organisation_id": str(self.organisation_id),
                                "deal_id": str(deal_id),
                                "error": str(e),
                            },
                        )

                await session.commit()
                await self._dispatch_pending_research()

        logger.info(
            "Org stakeholder sync complete: org=%s deals=%d/%d created=%d updated=%d",
            self.organisation_id,
            result.successful_deals,
            result.total_deals,
            result.stakeholders_created,
            result.stakeholders_updated,
            extra={
                "event": "stakeholder_sync_complete",
                "organisation_id": str(self.organisation_id),
                "successful_deals": result.successful_deals,
                "failed_deals": result.failed_deals,
                "total_deals": result.total_deals,
                "created_count": result.stakeholders_created,
                "updated_count": result.stakeholders_updated,
                "deactivated_count": result.stakeholders_deactivated,
                "rejected_count": result.stakeholders_rejected,
            },
        )

        return result

    def _iter_batches(self, deal_ids: list[UUID]) -> list[list[UUID]]:
        """Split org-wide sync work into bounded transactions."""
        return [
            deal_ids[index : index + self._org_sync_batch_size]
            for index in range(0, len(deal_ids), self._org_sync_batch_size)
        ]

    def _deal_savepoint(self, session: AsyncSession):
        """Use per-deal savepoints when the session supports them."""
        begin_nested = getattr(session, "begin_nested", None)
        if begin_nested is not None:
            return begin_nested()

        class _NoopSavepoint:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
                return False

        return _NoopSavepoint()

    async def sync_for_deal(self, deal_id: UUID) -> StakeholderSyncResult:
        """Sync stakeholders for a deal from CRMDealParticipant records.

        Acquires and releases its own session for this deal sync.

        Only processes participants where participant_type = CONTACT.

        Args:
            deal_id: UUID of the deal to sync

        Returns:
            StakeholderSyncResult with counts and requires_rematching flag
        """
        async for session in acquire_org_async_session(self.organisation_id):
            try:
                result = await self._sync_deal_internal(session, deal_id)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

            logger.info(
                "Stakeholder sync for deal %s: created=%d updated=%d deactivated=%d skipped=%d",
                deal_id,
                result.created,
                result.updated,
                result.deactivated,
                result.skipped,
                extra={
                    "event": "stakeholder_sync_result",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "created_count": result.created,
                    "updated_count": result.updated,
                    "deactivated_count": result.deactivated,
                    "skipped_count": result.skipped,
                },
            )
            await self._dispatch_pending_research()
            return result

        raise RuntimeError("Failed to acquire organization session")

    async def _sync_deal_internal(
        self, session: AsyncSession, deal_id: UUID
    ) -> StakeholderSyncResult:
        """Internal method to sync stakeholders for a deal.

        Used by both sync_for_deal (with passed session) and
        sync_for_organisation (with fresh session per deal).

        Args:
            session: Database session to use
            deal_id: UUID of the deal to sync

        Returns:
            StakeholderSyncResult with counts (does NOT commit - caller handles it)
        """
        # Load deal with participants
        deal = await self._load_deal(session, deal_id)
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
        existing_stakeholders = await self._load_stakeholders(session, deal_id)
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
                session,
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
                session.add(stakeholder)
                result.deactivated += 1

        # Reject stakeholders whose email domain matches the org's internal domains.
        # This self-cleans internal employees accidentally added as stakeholders
        # (e.g., by AI agents). Runs every sync cycle so the DB converges to clean state.
        result.rejected = await self._reject_internal_stakeholders(session, deal_id)

        return result

    async def _load_deal(self, session: AsyncSession, deal_id: UUID) -> CRMDeal | None:
        """Load deal with participants.

        Args:
            session: Database session to use
            deal_id: UUID of the deal to load
        """
        deal_participants_rel = cast(Any, CRMDeal.participants)
        stmt = (
            select(CRMDeal)
            .where(
                col(CRMDeal.deal_id) == deal_id,
                col(CRMDeal.organisation_id) == self.organisation_id,
            )
            .options(selectinload(deal_participants_rel))
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def _load_stakeholders(
        self, session: AsyncSession, deal_id: UUID
    ) -> list[DealStakeholder]:
        """Load existing stakeholders for a deal.

        Args:
            session: Database session to use
            deal_id: UUID of the deal
        """
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _ensure_stakeholder(
        self,
        session: AsyncSession,
        deal: CRMDeal,
        participant: CRMDealParticipant,
        existing: DealStakeholder | None = None,
    ) -> bool:
        """Create or update a stakeholder from a CRMDealParticipant.

        Args:
            session: Database session to use
            deal: The CRMDeal record
            participant: The CRMDealParticipant record (must be type=CONTACT)
            existing: Existing DealStakeholder if found by email

        Returns:
            True if created new stakeholder, False if updated existing.
            Returns False without changes if stakeholder was REMOVED by user.
        """
        if not participant.related_email:
            logger.warning(
                "Participant %s has no related_email, skipping",
                participant.participant_id,
                extra={
                    "event": "stakeholder_sync_participant_missing_email",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal.deal_id),
                    "participant_id": str(participant.participant_id),
                },
            )
            return False
        email = participant.related_email.lower()

        if existing:
            # Respect user decision: if stakeholder was REMOVED, don't reactivate
            if existing.approval_status == ApprovalStatus.REMOVED:
                return False

            manual_overrides = self._get_manual_overrides(existing)
            changed = False

            if (
                existing.source != StakeholderSource.MANUAL
                and existing.source != StakeholderSource.CRM
            ):
                existing.source = StakeholderSource.CRM
                changed = True
            if existing.source_reference_id != participant.participant_id:
                existing.source_reference_id = participant.participant_id
                changed = True
            if existing.approval_status != ApprovalStatus.REJECTED:
                if existing.approval_status != ApprovalStatus.APPROVED:
                    existing.approval_status = ApprovalStatus.APPROVED
                    changed = True
                # Reactivate if was soft-deleted
                if existing.is_deleted:
                    existing.is_deleted = False
                    changed = True

            # Preserve manual user edits; only auto-fill when the caller has not
            # overridden the name.
            if "name" not in manual_overrides and participant.related_name and not existing.name:
                existing.name = participant.related_name
                changed = True

            # NOTE: Do NOT set job_title from participant.relationship_role
            # relationship_role contains HubSpot association type (e.g., "deal_to_contact")
            # job_title should come from CRMContact.job_title via enrichment

            # Enrich from CRMContact (fixes bad job_title values from old sync)
            changed = (
                await self._enrich_from_contact(
                    session,
                    existing,
                    email,
                    manual_overrides=manual_overrides,
                )
                or changed
            )

            if changed:
                existing.updated_at = datetime.now(UTC)
                session.add(existing)

            return False

        # Seed stable identity fields first (email, source mapping, source_reference)
        # then enrich optional attributes from CRMContact. This keeps existing
        # stakeholders deterministic even if enrichment data is delayed.
        # NOTE: job_title comes from CRMContact, not participant.relationship_role.
        # relationship_role contains HubSpot association type (e.g., "deal_to_contact")
        # job_title will be enriched from CRMContact.job_title below
        stakeholder = DealStakeholder()
        stakeholder.organisation_id = self.organisation_id
        stakeholder.integration_id = deal.integration_id
        stakeholder.deal_id = deal.deal_id
        stakeholder.email = email
        stakeholder.name = participant.related_name
        stakeholder.source = StakeholderSource.CRM
        stakeholder.source_reference_id = participant.participant_id
        stakeholder.approval_status = ApprovalStatus.APPROVED  # CRM is always trusted
        stakeholder.job_title = None  # Will be enriched from CRMContact
        stakeholder.stance = StakeholderStance.UNKNOWN
        stakeholder.temperature = StakeholderTemperature.UNKNOWN
        stakeholder.engagement_score = 0.0
        stakeholder.last_contact_at = None

        # Enrich from CRMContact if available
        await self._enrich_from_contact(session, stakeholder, email)

        session.add(stakeholder)
        await session.flush()  # Generate stakeholder_id
        self._pending_research.append((stakeholder.stakeholder_id, deal.deal_id))
        return True

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
                            "event": "stakeholder_sync_research_dispatch_failed",
                            "organisation_id": str(self.organisation_id),
                            "stakeholder_id": str(stakeholder_id),
                            "deal_id": str(deal_id),
                        },
                    )
        finally:
            self._pending_research.clear()

    async def _enrich_from_contact(
        self,
        session: AsyncSession,
        stakeholder: DealStakeholder,
        email: str,
        manual_overrides: set[str] | None = None,
    ) -> bool:
        """Enrich stakeholder with CRMContact data if available.

        CRMContact is authoritative for contact linkage and default attributes,
        but user-managed fields in manual_overrides are never overwritten.

        Args:
            session: Database session to use
            stakeholder: The stakeholder to enrich
            email: Email address to look up
        """
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.email) == email,
        )
        result = await session.execute(stmt)
        contact = result.scalars().first()

        changed = False
        override_fields = manual_overrides or set()

        if contact:
            if stakeholder.contact_id != contact.contact_id:
                stakeholder.contact_id = contact.contact_id
                changed = True
            if "name" not in override_fields and contact.full_name and not stakeholder.name:
                stakeholder.name = contact.full_name
                changed = True
            # Use CRMContact.job_title unless this field is manually controlled
            if (
                "job_title" not in override_fields
                and contact.job_title
                and stakeholder.job_title != contact.job_title
            ):
                stakeholder.job_title = contact.job_title
                changed = True
            if (
                "phone_number" not in override_fields
                and contact.phone
                and stakeholder.phone_number != contact.phone
            ):
                stakeholder.phone_number = contact.phone
                changed = True

        return changed

    async def _collect_internal_domains(self, session: AsyncSession) -> set[str]:
        """Collect the organisation's internal email domains.

        Tier 1: Organisation.domain (admin-configured, authoritative).
        Tier 2: Derived from USER participant emails across all deals (fallback).

        Cached after first call to avoid repeated DB lookups within one sync run.
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
                for d in domains_raw:
                    if not d or not d.strip():
                        continue
                    d_lower = d.strip().lower()
                    domains.add(d_lower)
                    # Strip www. prefix so "www.acme.com" also blocks "@acme.com"
                    if d_lower.startswith("www."):
                        domains.add(d_lower[4:])

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

    @staticmethod
    def _is_internal_email(email: str, internal_domains: set[str]) -> bool:
        """Check if an email belongs to an internal domain."""
        if not email or "@" not in email:
            return False
        domain = email.split("@")[-1].lower()
        return domain in internal_domains

    async def _reject_internal_stakeholders(self, session: AsyncSession, deal_id: UUID) -> int:
        """Reject stakeholders whose email domain matches the org's internal domains.

        Called at the end of every stakeholder sync to self-clean internal employees
        that were accidentally added (e.g., by AI agents). Sets approval_status=REJECTED
        so they are excluded from meeting matching but preserved for audit trail.
        """
        internal_domains = await self._collect_internal_domains(session)
        if not internal_domains:
            return 0

        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.is_deleted).is_(False),
            col(DealStakeholder.approval_status) != ApprovalStatus.REJECTED,
        )
        result = await session.execute(stmt)
        stakeholders = list(result.scalars().all())

        count = 0
        for stakeholder in stakeholders:
            if self._is_internal_email(stakeholder.email, internal_domains):
                stakeholder.approval_status = ApprovalStatus.REJECTED
                count += 1

        if count > 0:
            logger.info(
                "Rejected %d internal-domain stakeholders for deal %s",
                count,
                deal_id,
                extra={
                    "event": "stakeholder_sync_rejected_internal_domains",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "rejected_count": count,
                },
            )

        return count
