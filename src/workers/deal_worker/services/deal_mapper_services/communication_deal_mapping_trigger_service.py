"""Resolve changed communications into bounded deal stakeholder remap jobs.

Called by email/calendar integration sync tasks after initial sync, incremental
sync, manual sync, and webhook-triggered sync. This module owns only the
communication-to-deal candidate selection and job dispatch boundary; the actual
stakeholder, calendar, and email rematching work runs in deal-worker tasks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    Email,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.observability.context import correlation_headers
from src.workers.deal_worker.client import send_deal_task_async
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_downstream_job_service import (
    EVENT_REMATCH_MAPPING_PURPOSE,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_eligibility import (
    DealMappingEligibilityService,
)
from src.workers.deal_worker.utils.pipeline_dispatch import AUTOMATED_PIPELINE_USER_ID

COMMUNICATION_SYNC_CHANGE_TYPE = "COMMUNICATION_SYNC_REMAP"
COMMUNICATION_SYNC_MAPPING_PURPOSE = EVENT_REMATCH_MAPPING_PURPOSE
COMMUNICATION_SYNC_SOURCE = "communication_sync_remap"

DEFAULT_RECORD_INSPECTION_CAP = 500
DEFAULT_DEAL_DISPATCH_CAP = 50
# Webhooks usually represent one provider delta, so keep both read and dispatch
# caps lower than batch sync. A single webhook must not reprocess all open deals.
WEBHOOK_RECORD_INSPECTION_CAP = 100
WEBHOOK_DEAL_DISPATCH_CAP = 10
EVENT_REMATCH_STALE_PENDING_MINUTES_ENV = "DEAL_MAPPING_EVENT_REMATCH_STALE_PENDING_MINUTES"
EVENT_REMATCH_STALE_IN_PROGRESS_MINUTES_ENV = "DEAL_MAPPING_EVENT_REMATCH_STALE_IN_PROGRESS_MINUTES"
DEFAULT_STALE_PENDING_JOB_MINUTES = 5
DEFAULT_STALE_IN_PROGRESS_JOB_MINUTES = 60

logger = logging.getLogger(__name__)

FREE_EMAIL_DOMAINS = {
    "aol.com",
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
    "ymail.com",
}

SourceKind = Literal["email", "calendar"]
TriggerSource = Literal["initial_sync", "incremental_sync", "manual_sync", "webhook"]


@dataclass(frozen=True)
class CommunicationRecord:
    """Minimal communication shape needed for deal candidate resolution."""

    record_id: UUID
    participant_emails: list[str]
    existing_deal_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class CandidateDeal:
    """Ranked deal candidate produced by participant matching."""

    deal_id: UUID
    rank: int
    reason: str
    changed_record_ids: list[UUID]


@dataclass
class CommunicationDealMappingTriggerResult:
    """Summary returned to thin task callers after communication remap dispatch."""

    inspected_records: int = 0
    skipped_internal_only: int = 0
    candidate_deals: int = 0
    eligible_deals: int = 0
    jobs_created: int = 0
    jobs_reused: int = 0
    jobs_dispatched: int = 0
    capped: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a Celery/log-friendly summary without exposing service internals."""
        return {
            "inspected_records": self.inspected_records,
            "skipped_internal_only": self.skipped_internal_only,
            "candidate_deals": self.candidate_deals,
            "eligible_deals": self.eligible_deals,
            "jobs_created": self.jobs_created,
            "jobs_reused": self.jobs_reused,
            "jobs_dispatched": self.jobs_dispatched,
            "capped": self.capped,
        }


class CommunicationDealMappingTriggerService:
    """Resolve changed emails/events to bounded deal-level stakeholder jobs.

    The service is used by integrations workers after communication sync. It
    deliberately keeps job creation here and mapping execution in deal-worker
    tasks so retries can reuse pending jobs instead of duplicating work.
    """

    def __init__(
        self,
        organisation_id: UUID,
        *,
        record_inspection_cap: int | None = None,
        deal_dispatch_cap: int | None = None,
    ) -> None:
        self.organisation_id = organisation_id
        self.record_inspection_cap = record_inspection_cap or DEFAULT_RECORD_INSPECTION_CAP
        self.deal_dispatch_cap = deal_dispatch_cap or DEFAULT_DEAL_DISPATCH_CAP
        self._eligibility = DealMappingEligibilityService(organisation_id)

    async def trigger_for_changed_communications(
        self,
        *,
        integration_id: UUID,
        source_kind: SourceKind,
        trigger_source: TriggerSource,
        changed_record_ids: list[UUID] | None = None,
        sync_started_at: datetime | None = None,
    ) -> CommunicationDealMappingTriggerResult:
        """Create stakeholder-sync jobs for deals touched by changed communications.

        Batch sync can inspect a bounded set of changed records, but webhook
        sync must stay narrow because a single provider notification should not
        fan out through every deal owned by the receiving user.
        """
        record_cap, deal_cap = self._caps_for_trigger(trigger_source)
        records = await self._load_changed_records(
            integration_id=integration_id,
            source_kind=source_kind,
            changed_record_ids=list(changed_record_ids or []),
            sync_started_at=sync_started_at,
            limit=record_cap,
        )
        result = CommunicationDealMappingTriggerResult(
            inspected_records=len(records),
            capped=len(records) >= record_cap,
        )
        if not records:
            return result

        # Public users identify app-onboarded internal people, while org domains
        # identify the wider company population that may not be onboarded.
        participant_emails = {
            email
            for record in records
            for email in self._normalize_email_list(record.participant_emails)
        }
        public_user_emails = await self._load_public_user_emails(participant_emails)
        integration_emails = await self._load_integration_emails()
        internal_emails = public_user_emails | integration_emails
        internal_domains = await self._load_organisation_domains()

        record_matches = self._classify_records(
            records,
            internal_emails,
            internal_domains,
        )
        result.skipped_internal_only = record_matches.skipped_internal_only
        has_existing_associations = bool(record_matches.existing_deal_record_ids)
        if not has_existing_associations and (
            not record_matches.internal_emails or not record_matches.external_emails
        ):
            return result

        # Webhooks get a targeted lookup only. The batch path may start from
        # internal users, but doing that for one webhook can enqueue thousands of
        # unrelated deals for a single employee's email.
        if trigger_source == "webhook":
            deals = await self._load_targeted_open_deals_for_webhook(
                record_matches,
                limit=deal_cap,
            )
        else:
            internal_anchored_deals = await self._load_internal_anchored_open_deals(
                record_matches.internal_emails
            )
            associated_deals = await self._load_existing_associated_open_deals(
                set(record_matches.existing_deal_record_ids),
                limit=deal_cap,
            )
            deals = self._merge_candidate_deals(associated_deals, internal_anchored_deals)

        deal_internal_emails = {
            email
            for deal in deals
            for email in self._eligibility.collect_internal_user_emails(deal)
        }
        public_user_emails = public_user_emails | await self._load_public_user_emails(
            deal_internal_emails
        )
        candidates = self._rank_candidate_deals(
            deals,
            record_matches=record_matches,
            public_user_emails=public_user_emails,
        )
        result.candidate_deals = len(candidates)
        result.capped = result.capped or len(candidates) > deal_cap
        if result.capped:
            logger.info(
                "Communication deal-mapping trigger capped",
                extra={
                    "organisation_id": str(self.organisation_id),
                    "integration_id": str(integration_id),
                    "source_kind": source_kind,
                    "trigger_source": trigger_source,
                    "inspected_records": result.inspected_records,
                    "candidate_deals": result.candidate_deals,
                    "deal_dispatch_cap": deal_cap,
                },
            )

        limited_candidates = candidates[:deal_cap]
        result.eligible_deals = len(limited_candidates)
        if not limited_candidates:
            return result

        claimed_jobs = await self._claim_stakeholder_jobs_for_deals(
            deal_ids=[candidate.deal_id for candidate in limited_candidates],
            integration_id=integration_id,
            source_kind=source_kind,
            trigger_source=trigger_source,
            candidate_reason_by_deal_id={
                candidate.deal_id: candidate.reason for candidate in limited_candidates
            },
            changed_record_sample_by_deal_id={
                candidate.deal_id: [
                    str(record_id) for record_id in candidate.changed_record_ids[:5]
                ]
                for candidate in limited_candidates
            },
        )

        dispatch_headers = correlation_headers()
        for job in claimed_jobs:
            if job["created"]:
                result.jobs_created += 1
                await send_deal_task_async(
                    "stakeholder.sync_deal_stakeholders",
                    kwargs={
                        "organisation_id": str(self.organisation_id),
                        "deal_id": job["deal_id"],
                        "change_type": COMMUNICATION_SYNC_CHANGE_TYPE,
                        "integration_id": str(integration_id),
                        "trigger_rematching": True,
                        "job_id": job["job_id"],
                    },
                    queue="deal",
                    routing_key="deal",
                    headers=dispatch_headers,
                )
                result.jobs_dispatched += 1
            else:
                result.jobs_reused += 1

        return result

    def _caps_for_trigger(self, trigger_source: TriggerSource) -> tuple[int, int]:
        if trigger_source == "webhook":
            return (
                min(self.record_inspection_cap, WEBHOOK_RECORD_INSPECTION_CAP),
                min(self.deal_dispatch_cap, WEBHOOK_DEAL_DISPATCH_CAP),
            )
        return self.record_inspection_cap, self.deal_dispatch_cap

    async def _load_changed_records(
        self,
        *,
        integration_id: UUID,
        source_kind: SourceKind,
        changed_record_ids: list[UUID],
        sync_started_at: datetime | None,
        limit: int,
    ) -> list[CommunicationRecord]:
        """Load the changed communication rows that the integration task just synced."""
        if source_kind == "email":
            return await self._load_changed_email_records(
                integration_id=integration_id,
                changed_record_ids=changed_record_ids,
                sync_started_at=sync_started_at,
                limit=limit,
            )
        return await self._load_changed_calendar_records(
            integration_id=integration_id,
            changed_record_ids=changed_record_ids,
            sync_started_at=sync_started_at,
            limit=limit,
        )

    async def _load_changed_email_records(
        self,
        *,
        integration_id: UUID,
        changed_record_ids: list[UUID],
        sync_started_at: datetime | None,
        limit: int,
    ) -> list[CommunicationRecord]:
        """Load changed email rows and any existing deal links for those messages."""
        async for session in acquire_org_async_session(self.organisation_id):
            # Tenant isolation is enforced with organisation_id; integration_id
            # alone is an external/provider-facing identifier and is not trusted.
            stmt = select(Email).where(
                col(Email.organisation_id) == self.organisation_id,
                col(Email.integration_id) == integration_id,
            )
            if changed_record_ids:
                stmt = stmt.where(col(Email.email_id).in_(changed_record_ids))
            elif sync_started_at is not None:
                stmt = stmt.where(col(Email.synced_at) >= sync_started_at)
            else:
                return []
            stmt = stmt.order_by(col(Email.synced_at).desc()).limit(limit)
            result = await session.execute(stmt)
            emails = list(result.scalars().all())

            existing_deals_by_message_id: dict[str, list[UUID]] = {}
            # Existing associations are the strongest signal for webhook remaps:
            # if a message is already linked, only that deal needs refreshing.
            message_ids = [
                email.internet_message_id for email in emails if email.internet_message_id
            ]
            if message_ids:
                assoc_stmt = select(DealEmailAssociation).where(
                    col(DealEmailAssociation.organisation_id) == self.organisation_id,
                    col(DealEmailAssociation.internet_message_id).in_(message_ids),
                    col(DealEmailAssociation.status) == AssociationStatus.ACTIVE,
                )
                assoc_result = await session.execute(assoc_stmt)
                for association in assoc_result.scalars().all():
                    existing_deals_by_message_id.setdefault(
                        association.internet_message_id, []
                    ).append(association.deal_id)

            return [
                CommunicationRecord(
                    record_id=email.email_id,
                    participant_emails=[
                        email.from_email,
                        *(email.to_emails or []),
                        *(email.cc_emails or []),
                        *(email.bcc_emails or []),
                    ],
                    existing_deal_ids=(
                        [email.deal_id]
                        if email.deal_id
                        else existing_deals_by_message_id.get(email.internet_message_id or "", [])
                    ),
                )
                for email in emails
            ]
        return []

    async def _load_changed_calendar_records(
        self,
        *,
        integration_id: UUID,
        changed_record_ids: list[UUID],
        sync_started_at: datetime | None,
        limit: int,
    ) -> list[CommunicationRecord]:
        """Load changed calendar event rows and existing deal links for those events."""
        attendees_rel = cast(Any, CalendarEvent.attendees)
        associations_rel = cast(Any, CalendarEvent.deal_associations)
        async for session in acquire_org_async_session(self.organisation_id):
            # Calendar events are scoped by both org and integration user state.
            # The org predicate is the tenant boundary; the integration ID only
            # tells us which synced account observed the event.
            stmt = (
                select(CalendarEvent)
                .join(
                    CalendarEventUserState,
                    col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
                )
                .where(
                    col(CalendarEvent.organisation_id) == self.organisation_id,
                    col(CalendarEventUserState.integration_id) == integration_id,
                    col(CalendarEventUserState.is_deleted).is_(False),
                )
                .options(selectinload(attendees_rel), selectinload(associations_rel))
            )
            if changed_record_ids:
                stmt = stmt.where(col(CalendarEvent.event_id).in_(changed_record_ids))
            elif sync_started_at is not None:
                stmt = stmt.where(col(CalendarEvent.synced_at) >= sync_started_at)
            else:
                return []
            stmt = stmt.order_by(col(CalendarEvent.synced_at).desc()).limit(limit)
            result = await session.execute(stmt)
            events = list(result.scalars().unique().all())

            return [
                CommunicationRecord(
                    record_id=event.event_id,
                    participant_emails=[
                        event.organiser_email,
                        *[attendee.email for attendee in event.attendees],
                    ],
                    existing_deal_ids=[
                        association.deal_id
                        for association in event.deal_associations
                        if association.status == AssociationStatus.ACTIVE
                    ],
                )
                for event in events
            ]
        return []

    async def _load_public_user_emails(self, emails: set[str]) -> set[str]:
        """Resolve participant emails that belong to onboarded app users."""
        if not emails:
            return set()
        async for session in acquire_master_async_session():
            stmt = select(col(User.email)).where(
                col(User.organisation_id) == self.organisation_id,
                func.lower(col(User.email)).in_(emails),
            )
            result = await session.execute(stmt)
            return {
                email
                for (raw_email,) in result.all()
                if (email := self._normalize_email(raw_email)) is not None
            }
        return set()

    async def _load_integration_emails(self) -> set[str]:
        """Load connected mailbox/calendar account emails for internal detection."""
        async for session in acquire_org_async_session(self.organisation_id):
            # Integration emails live in the org schema, so keep the org filter
            # even though the session search_path already points at one tenant.
            stmt = select(col(KnowledgeIntegration.integration_email)).where(
                col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.integration_email).isnot(None),
            )
            result = await session.execute(stmt)
            return {
                email
                for (raw_email,) in result.all()
                if (email := self._normalize_email(raw_email)) is not None
            }
        return set()

    async def _load_organisation_domains(self) -> set[str]:
        """Load authoritative internal domains configured on the public org row."""
        async for session in acquire_master_async_session():
            stmt = select(col(Organisation.domain)).where(
                col(Organisation.organisation_id) == self.organisation_id
            )
            result = await session.execute(stmt)
            domains_raw = result.scalar_one_or_none()
            if not isinstance(domains_raw, list):
                return set()
            return {
                domain
                for raw_domain in domains_raw
                if (domain := self._normalize_domain(raw_domain))
                and domain not in FREE_EMAIL_DOMAINS
            }
        return set()

    async def _load_internal_anchored_open_deals(self, internal_emails: set[str]) -> list[CRMDeal]:
        """Find open deals anchored to internal app users for batch sync remaps."""
        if not internal_emails:
            return []

        participants_rel = cast(Any, CRMDeal.participants)
        internal_participant_deal_ids = (
            select(col(CRMDealParticipant.deal_id))
            .where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.USER,
                col(CRMDealParticipant.is_deleted).is_(False),
                func.lower(col(CRMDealParticipant.related_email)).in_(internal_emails),
            )
            .scalar_subquery()
        )
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                self._eligibility.build_open_deals_query()
                .where(
                    or_(
                        func.lower(col(CRMDeal.owner_email)).in_(internal_emails),
                        col(CRMDeal.deal_id).in_(internal_participant_deal_ids),
                    )
                )
                .options(selectinload(participants_rel))
            )
            result = await session.execute(stmt)
            return list(result.scalars().unique().all())
        return []

    async def _load_targeted_open_deals_for_webhook(
        self,
        record_matches: Any,
        *,
        limit: int,
    ) -> list[CRMDeal]:
        """Find webhook candidate deals without broad internal-user fanout.

        Webhooks use only existing communication links and exact external
        contact email matches. Domain matching is intentionally excluded here
        because one customer domain can belong to many open deals.
        """
        existing_deal_ids = set(record_matches.existing_deal_record_ids)
        external_emails = set(record_matches.external_emails)
        if not existing_deal_ids and not external_emails:
            return []

        filters = []
        if existing_deal_ids:
            # Existing deal links are already tenant-scoped by the earlier
            # communication load, but the final open-deal query still applies
            # the org predicate through build_open_deals_query().
            filters.append(col(CRMDeal.deal_id).in_(existing_deal_ids))

        if external_emails:
            contact_participant_deal_ids = (
                select(col(CRMDealParticipant.deal_id))
                .where(
                    col(CRMDealParticipant.organisation_id) == self.organisation_id,
                    col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
                    col(CRMDealParticipant.is_deleted).is_(False),
                    func.lower(col(CRMDealParticipant.related_email)).in_(external_emails),
                )
                .scalar_subquery()
            )
            filters.extend(
                [
                    func.lower(col(CRMDeal.primary_contact_email)).in_(external_emails),
                    col(CRMDeal.deal_id).in_(contact_participant_deal_ids),
                ]
            )

        participants_rel = cast(Any, CRMDeal.participants)
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                self._eligibility.build_open_deals_query()
                .where(or_(*filters))
                .options(selectinload(participants_rel))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().unique().all())
        return []

    async def _load_existing_associated_open_deals(
        self,
        deal_ids: set[UUID],
        *,
        limit: int,
    ) -> list[CRMDeal]:
        """Load open deals already linked to the changed communication records."""
        if not deal_ids:
            return []

        async for session in acquire_org_async_session(self.organisation_id):
            result = await session.execute(
                self._eligibility.build_open_deals_query(deal_ids).limit(limit)
            )
            return list(result.scalars().unique().all())
        return []

    @staticmethod
    def _merge_candidate_deals(*deal_groups: list[CRMDeal]) -> list[CRMDeal]:
        merged: dict[UUID, CRMDeal] = {}
        for deals in deal_groups:
            for deal in deals:
                merged.setdefault(deal.deal_id, deal)
        return list(merged.values())

    def _classify_records(
        self,
        records: list[CommunicationRecord],
        internal_emails: set[str],
        internal_domains: set[str],
    ) -> Any:
        """Split communication participants into internal and external signals."""
        record_ids_by_external_email: dict[str, list[UUID]] = {}
        existing_deal_record_ids: dict[UUID, list[UUID]] = {}
        all_internal_emails: set[str] = set()
        all_external_emails: set[str] = set()
        skipped_internal_only = 0

        for record in records:
            participants = self._normalize_email_list(record.participant_emails)
            # Internal means either an exact app/integration email or the wider
            # org domain. This skips company-only meetings even when many
            # employees are not onboarded in public.users.
            record_internal = {
                email
                for email in participants
                if email in internal_emails or self._email_domain(email) in internal_domains
            }
            record_external = participants - record_internal
            for deal_id in record.existing_deal_ids:
                existing_deal_record_ids.setdefault(deal_id, []).append(record.record_id)
            if not record_external:
                if not record.existing_deal_ids:
                    skipped_internal_only += 1
                continue

            all_internal_emails.update(record_internal)
            all_external_emails.update(record_external)
            for email in record_external:
                record_ids_by_external_email.setdefault(email, []).append(record.record_id)

        return SimpleMatchContext(
            internal_emails=all_internal_emails,
            external_emails=all_external_emails,
            external_domains={
                domain
                for email in all_external_emails
                if (domain := self._email_domain(email))
                and domain not in FREE_EMAIL_DOMAINS
                and domain not in internal_domains
            },
            record_ids_by_external_email=record_ids_by_external_email,
            existing_deal_record_ids=existing_deal_record_ids,
            skipped_internal_only=skipped_internal_only,
        )

    def _rank_candidate_deals(
        self,
        deals: list[Any],
        *,
        record_matches: Any,
        public_user_emails: set[str],
    ) -> list[CandidateDeal]:
        """Rank loaded deals by the strength of their communication match."""
        candidates: list[CandidateDeal] = []

        for deal in deals:
            eligibility = self._eligibility.evaluate_loaded_deal(
                deal,
                public_user_emails=public_user_emails,
            )
            if not eligibility.is_eligible:
                continue

            deal_id = cast(UUID, deal.deal_id)
            if deal_id in record_matches.existing_deal_record_ids:
                # A persisted email/event association is stronger than matching
                # by participant text because an earlier matcher already linked
                # the communication to this deal.
                candidates.append(
                    CandidateDeal(
                        deal_id=deal_id,
                        rank=1,
                        reason="existing_communication_association",
                        changed_record_ids=record_matches.existing_deal_record_ids[deal_id],
                    )
                )
                continue

            deal_internal_emails = set(eligibility.matching_public_user_emails)
            if not deal_internal_emails.intersection(record_matches.internal_emails):
                continue

            exact_external_emails = self._deal_external_emails(deal)
            matching_emails = exact_external_emails.intersection(record_matches.external_emails)
            if matching_emails:
                reason = (
                    "exact_primary_contact_and_internal_user"
                    if self._normalize_email(getattr(deal, "primary_contact_email", None))
                    in matching_emails
                    else "exact_external_contact_and_internal_user"
                )
                candidates.append(
                    CandidateDeal(
                        deal_id=deal_id,
                        rank=2 if reason == "exact_external_contact_and_internal_user" else 3,
                        reason=reason,
                        changed_record_ids=self._record_ids_for_emails(
                            matching_emails,
                            record_matches.record_ids_by_external_email,
                        ),
                    )
                )
                continue

            deal_domain = self._normalize_domain(getattr(deal, "company_domain", None))
            if deal_domain and deal_domain in record_matches.external_domains:
                # Domain matching is kept for batch sync only. The webhook
                # loader never returns domain-only candidates, avoiding broad
                # fanout for a single provider notification.
                candidates.append(
                    CandidateDeal(
                        deal_id=deal_id,
                        rank=4,
                        reason="external_domain_and_internal_user",
                        changed_record_ids=[],
                    )
                )

        candidates.sort(key=lambda candidate: (candidate.rank, str(candidate.deal_id)))
        return candidates

    async def _claim_stakeholder_jobs_for_deals(
        self,
        *,
        deal_ids: list[UUID],
        integration_id: UUID,
        source_kind: SourceKind,
        trigger_source: TriggerSource,
        candidate_reason_by_deal_id: dict[UUID, str],
        changed_record_sample_by_deal_id: dict[UUID, list[str]],
    ) -> list[dict[str, Any]]:
        """Create or reuse pending stakeholder jobs for the chosen deals."""
        claimed_jobs: list[dict[str, Any]] = []
        async for session in acquire_org_async_session(self.organisation_id):
            existing_jobs = await self._load_existing_stakeholder_jobs(session, deal_ids)
            for deal_id in deal_ids:
                existing_job = existing_jobs.get(deal_id)
                if existing_job:
                    # Reusing pending/in-progress jobs makes retries and repeated
                    # sync notifications idempotent at the deal level.
                    claimed_jobs.append(
                        {
                            "deal_id": str(deal_id),
                            "job_id": str(existing_job.job_id),
                            "created": False,
                        }
                    )
                    continue

                job = DealJobs(
                    organisation_id=self.organisation_id,
                    user_id=AUTOMATED_PIPELINE_USER_ID,
                    job_type=DealJobType.STAKEHOLDER_SYNC,
                    deal_id=deal_id,
                    event_id=None,
                    status=JobStatus.PENDING,
                    job_metadata={
                        "source": COMMUNICATION_SYNC_SOURCE,
                        "mapping_purpose": COMMUNICATION_SYNC_MAPPING_PURPOSE,
                        "deal_id": str(deal_id),
                        "source_kind": source_kind,
                        "trigger_source": trigger_source,
                        "integration_id": str(integration_id),
                        "candidate_reason": candidate_reason_by_deal_id.get(deal_id),
                        "changed_record_sample": changed_record_sample_by_deal_id.get(deal_id, []),
                        "created_from_change_type": COMMUNICATION_SYNC_CHANGE_TYPE,
                    },
                )
                session.add(job)
                claimed_jobs.append(
                    {
                        "deal_id": str(deal_id),
                        "job_id": str(job.job_id),
                        "created": True,
                    }
                )

            if any(job["created"] for job in claimed_jobs):
                # Commit once after all claims are staged so either the batch of
                # new job rows is visible together or no dispatch metadata is.
                await session.commit()
            break
        return claimed_jobs

    async def _load_existing_stakeholder_jobs(
        self,
        session: Any,
        deal_ids: list[UUID],
    ) -> dict[UUID, DealJobs]:
        """Return active communication-remap stakeholder jobs keyed by deal ID."""
        if not deal_ids:
            return {}
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == self.organisation_id,
            col(DealJobs.deal_id).in_(deal_ids),
            col(DealJobs.job_type) == DealJobType.STAKEHOLDER_SYNC,
            col(DealJobs.status).in_([JobStatus.PENDING, JobStatus.IN_PROGRESS]),
        )
        result = await session.execute(stmt)
        existing: dict[UUID, DealJobs] = {}
        for job in result.scalars().all():
            if job.status not in {JobStatus.PENDING, JobStatus.IN_PROGRESS}:
                continue
            if (job.job_metadata or {}).get("mapping_purpose") != (
                COMMUNICATION_SYNC_MAPPING_PURPOSE
            ):
                continue
            if self._is_stale_active_job(job):
                job.status = JobStatus.FAILED
                job.error_message = "Stale event-rematch job replaced"
                job.job_metadata = {
                    **(job.job_metadata or {}),
                    "stale_replaced": True,
                    "stale_replaced_at": datetime.now(UTC).isoformat(),
                }
                logger.info(
                    "Stale event-rematch stakeholder job replaced",
                    extra={
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(job.deal_id) if job.deal_id else None,
                        "job_id": str(job.job_id),
                        "job_status": job.status.value,
                    },
                )
                continue
            if job.deal_id:
                existing[job.deal_id] = job
        return existing

    @staticmethod
    def _is_stale_active_job(job: DealJobs) -> bool:
        now = datetime.now(UTC)
        created_at = CommunicationDealMappingTriggerService._as_aware_utc(job.created_at)
        updated_at = CommunicationDealMappingTriggerService._as_aware_utc(job.updated_at)
        if job.status == JobStatus.PENDING:
            threshold = _threshold_from_env(
                EVENT_REMATCH_STALE_PENDING_MINUTES_ENV,
                DEFAULT_STALE_PENDING_JOB_MINUTES,
            )
            return created_at is not None and now - created_at > threshold
        if job.status == JobStatus.IN_PROGRESS:
            reference = updated_at or created_at
            threshold = _threshold_from_env(
                EVENT_REMATCH_STALE_IN_PROGRESS_MINUTES_ENV,
                DEFAULT_STALE_IN_PROGRESS_JOB_MINUTES,
            )
            return reference is not None and now - reference > threshold
        return False

    @staticmethod
    def _as_aware_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _deal_external_emails(deal: Any) -> set[str]:
        emails: set[str] = set()
        primary = CommunicationDealMappingTriggerService._normalize_email(
            getattr(deal, "primary_contact_email", None)
        )
        if primary:
            emails.add(primary)
        for participant in getattr(deal, "participants", None) or []:
            if getattr(participant, "is_deleted", False):
                continue
            if getattr(participant, "participant_type", None) != CRMParticipantType.CONTACT:
                continue
            email = CommunicationDealMappingTriggerService._normalize_email(
                getattr(participant, "related_email", None)
            )
            if email:
                emails.add(email)
        return emails

    @staticmethod
    def _record_ids_for_emails(
        emails: set[str],
        record_ids_by_external_email: dict[str, list[UUID]],
    ) -> list[UUID]:
        seen: set[UUID] = set()
        record_ids: list[UUID] = []
        for email in emails:
            for record_id in record_ids_by_external_email.get(email, []):
                if record_id in seen:
                    continue
                record_ids.append(record_id)
                seen.add(record_id)
        return record_ids

    @staticmethod
    def _normalize_email_list(values: list[str | None]) -> set[str]:
        return {
            normalized
            for value in values
            if (normalized := CommunicationDealMappingTriggerService._normalize_email(value))
            is not None
        }

    @staticmethod
    def _normalize_email(value: str | None) -> str | None:
        if not value:
            return None
        normalized = str(value).strip().lower()
        return normalized or None

    @staticmethod
    def _email_domain(email: str) -> str | None:
        if "@" not in email:
            return None
        return CommunicationDealMappingTriggerService._normalize_domain(email.rsplit("@", 1)[1])

    @staticmethod
    def _normalize_domain(value: str | None) -> str | None:
        if not value:
            return None
        normalized = str(value).strip().lower()
        normalized = normalized.removeprefix("http://").removeprefix("https://")
        normalized = normalized.removeprefix("www.")
        normalized = normalized.split("/", 1)[0]
        return normalized or None


@dataclass(frozen=True)
class SimpleMatchContext:
    """Normalized participant signals shared by candidate lookup and ranking."""

    internal_emails: set[str]
    external_emails: set[str]
    external_domains: set[str]
    record_ids_by_external_email: dict[str, list[UUID]]
    existing_deal_record_ids: dict[UUID, list[UUID]]
    skipped_internal_only: int


def _threshold_from_env(env_name: str, default_minutes: int) -> timedelta:
    raw_value = os.getenv(env_name)
    if raw_value is None:
        return timedelta(minutes=default_minutes)
    try:
        minutes = int(raw_value)
    except ValueError:
        return timedelta(minutes=default_minutes)
    if minutes <= 0:
        return timedelta(minutes=default_minutes)
    return timedelta(minutes=minutes)
