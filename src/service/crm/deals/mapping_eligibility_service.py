"""Shared eligibility checks for CRM deal mapping."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStatus,
    CRMParticipantType,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.crm.utils.identity import normalize_email

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


class DealMappingSkipReason(StrEnum):
    """Reasons a deal is not allowed to enter mapping workers."""

    DEAL_NOT_FOUND = "deal_not_found"
    DEAL_NOT_OPEN = "deal_not_open"
    DEAL_DELETED = "deal_deleted"
    NO_INTERNAL_USERS = "no_internal_users"
    NO_APP_USER_ON_DEAL = "no_app_user_on_deal"
    INTERNAL_USERS_NOT_IN_PUBLIC_USERS = "internal_users_not_in_public_users"


@dataclass(frozen=True)
class DealMappingEligibilityResult:
    """Eligibility decision for one deal."""

    deal_id: UUID
    is_eligible: bool
    reason: DealMappingSkipReason | None = None
    internal_user_emails: list[str] = field(default_factory=list)
    matching_public_user_emails: list[str] = field(default_factory=list)
    missing_public_user_emails: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DealMappingEligibilityListResult:
    """Eligibility IDs plus bounded scan telemetry for batch claimers."""

    eligible_deal_ids: list[UUID]
    candidate_deals_scanned: int
    candidate_scan_limit: int | None
    candidate_scan_capped: bool


class DealMappingEligibilityService:
    """Resolve which deals may run stakeholder, calendar, and email mapping.

    `crm_users` can contain CRM-only users. This service therefore uses
    same-org `public.users` email membership as the app-user source of truth.
    """

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    def evaluate_loaded_deal(
        self,
        deal: Any | None,
        *,
        public_user_emails: set[str],
    ) -> DealMappingEligibilityResult:
        """Evaluate a loaded CRM deal without doing additional database I/O."""
        if deal is None or getattr(deal, "organisation_id", None) != self.organisation_id:
            return DealMappingEligibilityResult(
                deal_id=getattr(deal, "deal_id", UUID(int=0)),
                is_eligible=False,
                reason=DealMappingSkipReason.DEAL_NOT_FOUND,
            )

        deal_id = cast(UUID, deal.deal_id)
        internal_user_emails = self.collect_internal_user_emails(deal)

        if getattr(deal, "is_deleted", False):
            return DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=False,
                reason=DealMappingSkipReason.DEAL_DELETED,
                internal_user_emails=internal_user_emails,
            )

        if getattr(deal, "deal_status", None) != CRMDealStatus.OPEN:
            return DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=False,
                reason=DealMappingSkipReason.DEAL_NOT_OPEN,
                internal_user_emails=internal_user_emails,
            )

        if not internal_user_emails:
            return DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=False,
                reason=DealMappingSkipReason.NO_INTERNAL_USERS,
            )

        normalized_public_emails = {
            normalized
            for email in public_user_emails
            if (normalized := normalize_email(email)) is not None
        }
        matching_public_user_emails = [
            email for email in internal_user_emails if email in normalized_public_emails
        ]
        if not matching_public_user_emails:
            return DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=False,
                reason=DealMappingSkipReason.NO_APP_USER_ON_DEAL,
                internal_user_emails=internal_user_emails,
                missing_public_user_emails=internal_user_emails,
            )

        return DealMappingEligibilityResult(
            deal_id=deal_id,
            is_eligible=True,
            internal_user_emails=internal_user_emails,
            matching_public_user_emails=matching_public_user_emails,
        )

    def list_eligible_loaded_deal_ids(
        self,
        deals: Sequence[Any],
        *,
        public_user_emails: set[str],
    ) -> list[UUID]:
        """Return eligible IDs from already-loaded deals."""
        return [
            result.deal_id
            for deal in deals
            if (
                result := self.evaluate_loaded_deal(deal, public_user_emails=public_user_emails)
            ).is_eligible
        ]

    def collect_internal_user_emails(self, deal: Any) -> list[str]:
        """Collect owner and active USER participant emails in stable order."""
        emails: list[str] = []
        seen: set[str] = set()

        self._append_email(emails, seen, getattr(deal, "owner_email", None))

        for participant in getattr(deal, "participants", None) or []:
            if getattr(participant, "is_deleted", False):
                continue
            if getattr(participant, "participant_type", None) != CRMParticipantType.USER:
                continue
            self._append_email(emails, seen, getattr(participant, "related_email", None))

        return emails

    def build_open_deals_query(
        self,
        deal_ids: Iterable[UUID] | None = None,
        *,
        exclude_deal_ids: Iterable[UUID] | None = None,
        limit: int | None = None,
    ) -> Select[Any]:
        """Build the org-wide candidate query used by chunking coordinators."""
        deal_participants_rel = cast(Any, CRMDeal.participants)
        stmt = (
            select(CRMDeal)
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                col(CRMDeal.is_deleted).is_(False),
            )
            .options(selectinload(deal_participants_rel))
            .order_by(col(CRMDeal.deal_id))
        )
        if deal_ids is not None:
            stmt = stmt.where(col(CRMDeal.deal_id).in_(set(deal_ids)))
        excluded_ids = set(exclude_deal_ids or [])
        if excluded_ids:
            stmt = stmt.where(col(CRMDeal.deal_id).not_in(excluded_ids))
        if limit is not None:
            stmt = stmt.limit(limit)
        return stmt

    async def get_deal_eligibility(self, deal_id: UUID) -> DealMappingEligibilityResult:
        """Load one deal and evaluate whether mapper tasks should process it."""
        deal = None
        async for session in acquire_org_async_session(self.organisation_id):
            deal = await self._load_deal(session, deal_id)

        if deal is None:
            return DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=False,
                reason=DealMappingSkipReason.DEAL_NOT_FOUND,
            )

        internal_user_emails = self.collect_internal_user_emails(deal)
        public_user_emails = await self._load_public_user_emails(set(internal_user_emails))
        return self.evaluate_loaded_deal(deal, public_user_emails=public_user_emails)

    async def list_eligible_deal_ids(
        self,
        deal_ids: Iterable[UUID] | None = None,
        *,
        exclude_deal_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        """Load open deal candidates and return only eligible deal IDs."""
        result = await self.list_eligible_deal_ids_with_scan_metrics(
            deal_ids=deal_ids,
            exclude_deal_ids=exclude_deal_ids,
        )
        return result.eligible_deal_ids

    async def list_eligible_deal_ids_with_scan_metrics(
        self,
        deal_ids: Iterable[UUID] | None = None,
        *,
        exclude_deal_ids: Iterable[UUID] | None = None,
        candidate_scan_limit: int | None = None,
    ) -> DealMappingEligibilityListResult:
        """Load eligible deal IDs while optionally bounding open-deal candidates."""
        deals: list[CRMDeal] = []
        query_limit = candidate_scan_limit + 1 if candidate_scan_limit is not None else None
        async for session in acquire_org_async_session(self.organisation_id):
            query = self.build_open_deals_query(
                deal_ids,
                exclude_deal_ids=exclude_deal_ids,
                limit=query_limit,
            )
            result = await session.execute(query)
            deals = list(result.scalars().all())

        candidate_scan_capped = (
            candidate_scan_limit is not None and len(deals) > candidate_scan_limit
        )
        if candidate_scan_capped:
            deals = deals[:candidate_scan_limit]

        if not deals:
            return DealMappingEligibilityListResult(
                eligible_deal_ids=[],
                candidate_deals_scanned=0,
                candidate_scan_limit=candidate_scan_limit,
                candidate_scan_capped=False,
            )

        internal_emails = {
            email for deal in deals for email in self.collect_internal_user_emails(deal)
        }
        if not internal_emails:
            return DealMappingEligibilityListResult(
                eligible_deal_ids=[],
                candidate_deals_scanned=len(deals),
                candidate_scan_limit=candidate_scan_limit,
                candidate_scan_capped=candidate_scan_capped,
            )

        public_user_emails = await self._load_public_user_emails(internal_emails)
        return DealMappingEligibilityListResult(
            eligible_deal_ids=self.list_eligible_loaded_deal_ids(
                deals,
                public_user_emails=public_user_emails,
            ),
            candidate_deals_scanned=len(deals),
            candidate_scan_limit=candidate_scan_limit,
            candidate_scan_capped=candidate_scan_capped,
        )

    async def list_open_deal_ids(self) -> list[UUID]:
        """Return open, non-deleted deal IDs that are candidates for mapping."""
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(col(CRMDeal.deal_id))
                .where(
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                    col(CRMDeal.is_deleted).is_(False),
                )
                .order_by(col(CRMDeal.deal_id))
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]
        return []

    async def _load_deal(self, session: AsyncSession, deal_id: UUID) -> CRMDeal | None:
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

    async def _load_public_user_emails(self, emails: set[str]) -> set[str]:
        if not emails:
            return set()

        normalized_emails = {
            normalized for email in emails if (normalized := normalize_email(email)) is not None
        }
        if not normalized_emails:
            return set()

        async for session in acquire_master_async_session():
            stmt = select(col(User.email)).where(
                col(User.organisation_id) == self.organisation_id,
                func.lower(col(User.email)).in_(normalized_emails),
            )
            result = await session.execute(stmt)
            return {
                normalized
                for (email,) in result.all()
                if (normalized := normalize_email(str(email))) is not None
            }

        return set()

    def _append_email(self, emails: list[str], seen: set[str], raw_email: str | None) -> None:
        normalized = normalize_email(raw_email)
        if normalized is None or normalized in seen:
            return
        emails.append(normalized)
        seen.add(normalized)
