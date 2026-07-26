"""Participant-owned stakeholder normalization service.

The public task name still says "stakeholder sync", but the canonical deal-person
state is now `CRMDealParticipant`. This service normalizes contact participants,
links them to durable contacts, rejects internal participants, and returns
contact research candidates without writing the old stakeholder table.
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
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.utils.identity import extract_email_domain, normalize_email
from src.service.crm.utils.internal_domains import (
    is_internal_email,
    normalize_internal_domains,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


@dataclass
class StakeholderSyncResult:
    """Result of normalizing participant-backed stakeholders for a deal."""

    deal_id: UUID
    integration_id: UUID | None = None
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    rejected: int = 0
    skipped: int = 0
    replay_required: bool = False
    research_contact_ids: set[UUID] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return whether the deal sync completed without recorded errors."""
        return len(self.errors) == 0

    @property
    def requires_rematching(self) -> bool:
        """Return whether normalized participant state requires communication replay."""
        return self.replay_required


@dataclass
class OrgStakeholderSyncResult:
    """Result of normalizing participant-backed stakeholders for an organization."""

    organisation_id: UUID
    total_deals: int = 0
    successful_deals: int = 0
    failed_deals: int = 0
    stakeholders_created: int = 0
    stakeholders_updated: int = 0
    stakeholders_deactivated: int = 0
    stakeholders_rejected: int = 0
    replay_required: bool = False
    research_contact_ids: set[UUID] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return whether every selected deal synced successfully."""
        return self.failed_deals == 0

    @property
    def requires_rematching(self) -> bool:
        """Return whether any normalized deal requires communication replay."""
        return self.replay_required


class StakeholderSyncService:
    """Normalizes contact participants used as deal stakeholders."""

    def __init__(
        self,
        organisation_id: UUID,
        *,
        org_sync_batch_size: int = 100,
    ) -> None:
        self.organisation_id = organisation_id
        self._org_sync_batch_size = max(1, org_sync_batch_size)
        self._internal_domains: set[str] | None = None

    @staticmethod
    def _enum_value(value: Any) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "value", value))

    async def sync_for_organisation(self) -> OrgStakeholderSyncResult:
        """Normalize participant-backed stakeholders for every open deal in the org."""
        result = OrgStakeholderSyncResult(organisation_id=self.organisation_id)

        deal_ids: list[UUID] = []
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(col(CRMDeal.deal_id))
                .where(
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.is_deleted).is_(False),
                    col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                )
                .order_by(col(CRMDeal.deal_id))
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
            "Syncing participant-backed stakeholders for %d deals in org %s",
            result.total_deals,
            self.organisation_id,
            extra={
                "event": "stakeholder_sync_started",
                "organisation_id": str(self.organisation_id),
                "total_deals": result.total_deals,
            },
        )

        for batch in self._iter_batches(deal_ids):
            async for session in acquire_org_async_session(self.organisation_id):
                for deal_id in batch:
                    try:
                        async with self._deal_savepoint(session):
                            deal_result = await self._sync_deal_internal(session, deal_id)

                        result.successful_deals += 1
                        result.stakeholders_created += deal_result.created
                        result.stakeholders_updated += deal_result.updated
                        result.stakeholders_deactivated += deal_result.deactivated
                        result.stakeholders_rejected += deal_result.rejected
                        result.replay_required = (
                            result.replay_required or deal_result.replay_required
                        )
                        result.research_contact_ids.update(deal_result.research_contact_ids)

                        if deal_result.errors:
                            result.errors.extend(deal_result.errors[:3])

                    except Exception as exc:
                        result.failed_deals += 1
                        result.errors.append(f"Deal {deal_id}: {str(exc)}")
                        logger.warning(
                            "Failed to sync participant-backed stakeholders for deal %s: %s",
                            deal_id,
                            str(exc),
                            extra={
                                "event": "stakeholder_sync_failed_for_deal",
                                "organisation_id": str(self.organisation_id),
                                "deal_id": str(deal_id),
                                "error": str(exc),
                            },
                        )

                await session.commit()

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
        return [
            deal_ids[index : index + self._org_sync_batch_size]
            for index in range(0, len(deal_ids), self._org_sync_batch_size)
        ]

    def _deal_savepoint(self, session: AsyncSession):
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
        """Normalize participant-backed stakeholders for a single deal."""
        async for session in acquire_org_async_session(self.organisation_id):
            try:
                result = await self._sync_deal_internal(session, deal_id)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

            logger.info(
                "Stakeholder sync for deal %s: created=%d updated=%d rejected=%d skipped=%d",
                deal_id,
                result.created,
                result.updated,
                result.rejected,
                result.skipped,
                extra={
                    "event": "stakeholder_sync_result",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "created_count": result.created,
                    "updated_count": result.updated,
                    "rejected_count": result.rejected,
                    "skipped_count": result.skipped,
                },
            )
            return result

        raise RuntimeError("Failed to acquire organization session")

    async def _sync_deal_internal(
        self, session: AsyncSession, deal_id: UUID
    ) -> StakeholderSyncResult:
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
        seen_emails: set[str] = set()
        internal_domains = await self._collect_internal_domains(session)

        for participant in deal.participants or []:
            if self._enum_value(participant.participant_type) != CRMParticipantType.CONTACT.value:
                continue
            if participant.is_deleted:
                result.skipped += 1
                continue

            email = normalize_email(participant.related_email) or ""
            if not email:
                result.skipped += 1
                logger.debug("Skipped participant %s - no email", participant.participant_id)
                continue

            if email in seen_emails:
                result.skipped += 1
                logger.debug(
                    "Skipped participant %s - duplicate email %s",
                    participant.participant_id,
                    email,
                )
                continue
            seen_emails.add(email)

            if is_internal_email(email, internal_domains):
                if self._enum_value(participant.approval_status) != ApprovalStatus.REJECTED.value:
                    participant.approval_status = ApprovalStatus.REJECTED.value
                    participant.updated_at = datetime.now(UTC)
                    session.add(participant)
                    result.rejected += 1
                    result.replay_required = True
                continue

            if self._enum_value(participant.approval_status) in {
                ApprovalStatus.REJECTED.value,
                ApprovalStatus.REMOVED.value,
            }:
                result.skipped += 1
                continue

            changed = False
            if participant.related_email != email:
                participant.related_email = email
                changed = True

            changed = (
                await self._enrich_from_contact(
                    session=session,
                    stakeholder=participant,
                    email=email,
                )
                or changed
            )

            if changed:
                participant.updated_at = datetime.now(UTC)
                session.add(participant)
                result.updated += 1
                result.replay_required = True

            if contact_id := await self._contact_id_needing_research(session, participant):
                result.research_contact_ids.add(contact_id)

        return result

    async def _contact_id_needing_research(
        self,
        session: AsyncSession,
        participant: CRMDealParticipant,
    ) -> UUID | None:
        """Return the linked contact id when contact-owned research is missing.

        The linked CRMContact is authoritative after the contact-owned research
        migration; unlinked placeholders do not carry enough identity to run.
        """
        if not participant.related_local_id:
            return None

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id) == participant.related_local_id,
            col(CRMContact.is_deleted).is_(False),
        )
        result = await session.execute(stmt)
        contact = result.scalar_one_or_none()
        if not contact:
            return None
        if contact.contact_research or contact.last_researched_at:
            return None
        return contact.contact_id

    async def _load_deal(self, session: AsyncSession, deal_id: UUID) -> CRMDeal | None:
        deal_participants_rel = cast(Any, CRMDeal.participants)
        stmt = (
            select(CRMDeal)
            .where(
                col(CRMDeal.deal_id) == deal_id,
                col(CRMDeal.organisation_id) == self.organisation_id,
            )
            .options(selectinload(deal_participants_rel))
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def _enrich_from_contact(
        self,
        session: AsyncSession,
        stakeholder: Any,
        email: str,
        manual_overrides: set[str] | None = None,
    ) -> bool:
        _ = manual_overrides
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            col(CRMContact.email) == email,
        )
        result = await session.execute(stmt)
        contact = result.scalars().first()
        if not contact:
            return False

        changed = False
        changed |= self._set_if_different(stakeholder, "related_local_id", contact.contact_id)
        changed |= self._set_if_different(stakeholder, "contact_id", contact.contact_id)
        return changed

    @staticmethod
    def _set_if_different(row: Any, field_name: str, value: Any) -> bool:
        if value is None or not hasattr(row, field_name):
            return False
        if getattr(row, field_name) == value:
            return False
        setattr(row, field_name, value)
        return True

    async def _collect_internal_domains(self, session: AsyncSession) -> set[str]:
        if self._internal_domains is not None:
            return self._internal_domains

        domains: set[str] = set()
        async for master_session in acquire_master_async_session():
            org_stmt = select(col(Organisation.domain)).where(
                col(Organisation.organisation_id) == self.organisation_id,
            )
            org_result = await master_session.execute(org_stmt)
            domains_raw = org_result.scalar_one_or_none()
            if isinstance(domains_raw, list):
                domains.update(normalize_internal_domains(domains_raw))

        if not domains:
            participant_stmt = select(func.distinct(col(CRMDealParticipant.related_email))).where(
                col(CRMDealParticipant.participant_type) == CRMParticipantType.USER.value,
            )
            participant_result = await session.execute(participant_stmt)
            for (email,) in participant_result.all():
                if domain := extract_email_domain(email):
                    domains.add(domain)

        self._internal_domains = domains
        return domains

    async def _reject_internal_stakeholders(self, session: AsyncSession, deal_id: UUID) -> int:
        internal_domains = await self._collect_internal_domains(session)
        if not internal_domains:
            return 0

        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.approval_status) != ApprovalStatus.REJECTED.value,
        )
        result = await session.execute(stmt)
        participants = list(result.scalars().all())

        count = 0
        for participant in participants:
            email = normalize_email(participant.related_email)
            if is_internal_email(email, internal_domains):
                participant.approval_status = ApprovalStatus.REJECTED.value
                participant.updated_at = datetime.now(UTC)
                session.add(participant)
                count += 1

        if count > 0:
            logger.info(
                "Rejected %d internal-domain participants for deal %s",
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
