"""Hydrate committed communications into contact, account, and deal activity.

Integration sync calls the changed-source path after email/calendar rows commit.
Deal participant changes call the targeted deal path after stakeholder
normalization, while manual API and agent-tool contact creation call the
explicit-contact path. Committed CSV deal imports also use targeted deal
hydration rather than a separate mapper.

This service owns local history loading and orchestration: it resolves
communication identities to contacts, projects contact/account activity, asks
the association service to match eligible deals, writes confidently discovered
stakeholders, and hydrates newly approved contacts once. The recursive pass
disables further discovery so one communication cannot expand a deal's
stakeholder graph without bound.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.activities import CRMActivity
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation, Email
from src.service.crm.activity_feed_projection_service import (
    ActivityFeedProjectionService,
)
from src.service.crm.communications.deal_association_service import (
    CommunicationDealAssociationResult,
    CommunicationDealAssociationService,
)
from src.service.crm.communications.participant_resolution_service import (
    CommunicationParticipantResolution,
    CommunicationParticipantResolutionService,
    ContactCommunicationHydrationContext,
    contacts_for_participants,
    email_participants,
    event_participants,
    identity_contact_map,
)
from src.service.crm.deals.last_activity_service import DealLastActivityService
from src.service.crm.guards.internal_user_guard import FREE_EMAIL_DOMAINS
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService
from src.service.crm.utils.identity import normalize_email
from src.service.crm.utils.internal_domains import normalize_internal_domains

# copilotgtm: cap each retained-history query at 500 rows to bound ORM memory;
# replace offset paging only if measured contact histories make it a bottleneck.
HISTORY_PAGE_SIZE = 500


@dataclass(frozen=True)
class ContactCommunicationHydrationResult:
    """Compact worker result suitable for logs and Celery task responses."""

    emails_considered: int
    events_considered: int
    contacts_created: int
    contacts_reused: int
    activities_projected: int
    skip_counters: dict[str, int]
    accounts_created: int = 0
    emails_linked: int = 0
    identities_considered: int = 0
    deal_associations_created: int = 0
    deal_associations_ambiguous: int = 0
    deal_associations_deactivated: int = 0
    participants_auto_approved: int = 0
    participants_added_pending: int = 0
    participants_skipped_domain: int = 0
    discovered_contact_ids: frozenset[UUID] = field(default_factory=frozenset)
    email_deal_ids: frozenset[UUID] = field(default_factory=frozenset)
    attachment_email_ids_by_deal: dict[UUID, frozenset[UUID]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result without leaking ORM rows into Celery."""
        return {
            "emails_considered": self.emails_considered,
            "events_considered": self.events_considered,
            "contacts_created": self.contacts_created,
            "contacts_reused": self.contacts_reused,
            "accounts_created": self.accounts_created,
            "emails_linked": self.emails_linked,
            "activities_projected": self.activities_projected,
            "identities_considered": self.identities_considered,
            "deal_associations_created": self.deal_associations_created,
            "deal_associations_ambiguous": self.deal_associations_ambiguous,
            "deal_associations_deactivated": self.deal_associations_deactivated,
            "participants_auto_approved": self.participants_auto_approved,
            "participants_added_pending": self.participants_added_pending,
            "participants_skipped_domain": self.participants_skipped_domain,
            "skip_counters": self.skip_counters,
        }


def _merge_attachment_email_ids(
    target: dict[UUID, set[UUID]],
    source: dict[UUID, set[UUID] | frozenset[UUID]],
) -> None:
    """Merge deal-scoped attachment work while preserving exact source email IDs."""
    for deal_id, email_ids in source.items():
        target.setdefault(deal_id, set()).update(email_ids)


class ContactCommunicationHydrationService:
    """Run changed-source contact creation or explicit-contact activity backfill."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
    ) -> None:
        """Bind the master identity session and tenant-local write session for one batch."""
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id

    async def hydrate_changed_sources(
        self,
        *,
        integration_id: UUID | None = None,
        changed_email_ids: list[UUID] | None = None,
        changed_event_ids: list[UUID] | None = None,
    ) -> ContactCommunicationHydrationResult:
        """Hydrate one committed email/calendar change batch.

        This path does not run stakeholder normalization. It resolves all
        eligible participants, projects contact/account activity, creates deal
        associations that satisfy current stakeholder rules, then performs one
        non-recursive history expansion for newly approved stakeholders.
        """
        # --- Step 1: load only the committed source rows and retained thread context ---
        changed_emails = await self._load_changed_emails(changed_email_ids or [])
        events = await self._load_changed_events(changed_event_ids or [])
        # When a reply proves engagement, hydrate the retained thread as one relationship so
        # earlier senders and copied participants receive both a contact and their history.
        emails = await self._load_email_thread_context(changed_emails)
        resolution = await self._resolve_communication_participants(
            emails=emails,
            events=events,
            seed_contacts=[],
        )
        # --- Step 2: project relationship activity before selecting deal ownership ---
        linked_email_ids: set[UUID] = set()
        projected = await self._project(
            emails=emails,
            events=events,
            identity_contacts=resolution.identity_contacts,
            integration_id=integration_id,
            replace_links=True,
            target_deal_ids=None,
            linked_email_ids=linked_email_ids,
        )
        # --- Step 3: create qualifying deal links and collect discovery candidates ---
        # Deal association consumes the exact contacts resolved above. Running it after direct
        # projection keeps one transaction and avoids a second organisation-wide mapper scan.
        association_result = await CommunicationDealAssociationService(
            self.org_session,
            self.organisation_id,
        ).associate_changed_communications(
            emails=emails,
            events=events,
            identity_contacts=resolution.identity_contacts,
            email_contact_identities=resolution.plan.email_contact_identities,
            event_contact_identities=resolution.plan.event_contact_identities,
            integration_id=integration_id,
            attachment_scope_email_ids={email.email_id for email in changed_emails},
        )
        await self._write_stakeholder_candidates(association_result)
        # Auto-approved discoveries get exactly one history pass. Disabling discovery
        # in that pass prevents stakeholder-of-stakeholder recursion.
        if association_result.discovered_contact_ids:
            expansion = await self._backfill_contacts(
                sorted(association_result.discovered_contact_ids, key=str),
                integration_id=integration_id,
                discover_participants=False,
                target_deal_ids=None,
            )
            projected += expansion.activities_projected
            association_result.associations_created += expansion.deal_associations_created
            association_result.ambiguous += expansion.deal_associations_ambiguous
            association_result.discovered_contact_ids.update(expansion.discovered_contact_ids)
            association_result.email_deal_ids.update(expansion.email_deal_ids)
            _merge_attachment_email_ids(
                association_result.attachment_email_ids_by_deal,
                expansion.attachment_email_ids_by_deal,
            )
        return ContactCommunicationHydrationResult(
            emails_considered=len(emails),
            events_considered=len(events),
            contacts_created=len(resolution.created_contact_ids),
            contacts_reused=len(resolution.reused_contact_ids),
            accounts_created=len(resolution.created_account_ids),
            emails_linked=len(set(changed_email_ids or []) & linked_email_ids),
            activities_projected=projected,
            skip_counters=resolution.plan.skip_counters,
            identities_considered=len(resolution.plan.activity_identities),
            deal_associations_created=association_result.associations_created,
            deal_associations_ambiguous=association_result.ambiguous,
            deal_associations_deactivated=association_result.associations_deactivated,
            participants_auto_approved=association_result.participants_added_approved,
            participants_added_pending=association_result.participants_added_pending,
            participants_skipped_domain=association_result.participants_skipped_domain,
            discovered_contact_ids=frozenset(association_result.discovered_contact_ids),
            email_deal_ids=frozenset(association_result.email_deal_ids),
            attachment_email_ids_by_deal={
                deal_id: frozenset(email_ids)
                for deal_id, email_ids in association_result.attachment_email_ids_by_deal.items()
            },
        )

    async def backfill_contacts(
        self,
        contact_ids: list[UUID],
        *,
        integration_id: UUID | None = None,
    ) -> ContactCommunicationHydrationResult:
        """Attach retained history to explicit contacts and eligible deals.

        Manual/tool contact creation enters here after the contact exists. The
        first pass may discover same-domain deal stakeholders, followed by one
        non-recursive history pass for the newly approved contacts.
        """
        return await self._backfill_contacts(
            contact_ids,
            integration_id=integration_id,
            discover_participants=True,
            target_deal_ids=None,
        )

    async def _backfill_contacts(
        self,
        contact_ids: list[UUID],
        *,
        integration_id: UUID | None,
        discover_participants: bool,
        target_deal_ids: set[UUID] | None,
    ) -> ContactCommunicationHydrationResult:
        """Page through contact history and optionally allow one discovery generation.

        ``target_deal_ids`` narrows writes during deal replay but never widens
        eligibility: every association must still pass the approved-contact and
        internal-participant rules in the association service.
        """
        # Anchor identities come only from active, org-scoped contacts supplied by
        # the caller; communication co-participants are resolved separately per page.
        contacts = await self._load_contacts(contact_ids)
        if not contacts:
            return ContactCommunicationHydrationResult(
                emails_considered=0,
                events_considered=0,
                contacts_created=0,
                contacts_reused=0,
                activities_projected=0,
                skip_counters={},
            )
        anchor_identity_contacts = identity_contact_map(contacts)
        identities = set(anchor_identity_contacts)
        email_offset = 0
        event_offset = 0
        emails_complete = False
        events_complete = False
        emails_considered = 0
        events_considered = 0
        projected = 0
        created_contact_ids: set[UUID] = set()
        resolved_contact_ids = {contact.contact_id for contact in contacts}
        skip_counters: Counter[str] = Counter()
        activity_identities: set[str] = set()
        association_result = CommunicationDealAssociationResult()

        # Email and meeting histories advance independently because one source may
        # exhaust before the other. Stable ID ordering makes each offset deterministic.
        while not (emails_complete and events_complete):
            emails = (
                []
                if emails_complete
                else await self._load_emails_for_identities(
                    identities,
                    offset=email_offset,
                    limit=HISTORY_PAGE_SIZE,
                )
            )
            events = (
                []
                if events_complete
                else await self._load_events_for_identities(
                    identities,
                    offset=event_offset,
                    limit=HISTORY_PAGE_SIZE,
                )
            )
            emails_complete = emails_complete or len(emails) < HISTORY_PAGE_SIZE
            events_complete = events_complete or len(events) < HISTORY_PAGE_SIZE
            email_offset += len(emails)
            event_offset += len(events)
            if not emails and not events:
                continue

            # Resolve the full page before projection so contact/account activity and
            # deal matching consume the same participant interpretation.
            resolution = await self._resolve_communication_participants(
                emails=emails,
                events=events,
                seed_contacts=contacts,
            )
            emails_considered += len(emails)
            events_considered += len(events)
            created_contact_ids.update(resolution.created_contact_ids)
            resolved_contact_ids.update(
                contact.contact_id for contact in resolution.identity_contacts.values()
            )
            skip_counters.update(resolution.plan.skip_counters)
            activity_identities.update(resolution.plan.activity_identities)
            projected += await self._project(
                emails=emails,
                events=events,
                identity_contacts=resolution.identity_contacts,
                integration_id=integration_id,
                replace_links=False,
                target_deal_ids=target_deal_ids,
            )
            # Association follows direct projection in the same transaction. If it
            # rolls back, neither partial card ownership nor stakeholder writes commit.
            page_associations = await CommunicationDealAssociationService(
                self.org_session,
                self.organisation_id,
            ).associate_changed_communications(
                emails=emails,
                events=events,
                identity_contacts=resolution.identity_contacts,
                email_contact_identities=resolution.plan.email_contact_identities,
                event_contact_identities=resolution.plan.event_contact_identities,
                integration_id=integration_id,
                discover_participants=discover_participants,
                target_deal_ids=target_deal_ids,
                attachment_scope_email_ids={email.email_id for email in emails},
            )
            await self._write_stakeholder_candidates(page_associations)
            association_result.associations_created += page_associations.associations_created
            association_result.ambiguous += page_associations.ambiguous
            association_result.unmatched += page_associations.unmatched
            association_result.associations_deactivated += (
                page_associations.associations_deactivated
            )
            association_result.discovered_contact_ids.update(
                page_associations.discovered_contact_ids
            )
            association_result.participants_added_approved += (
                page_associations.participants_added_approved
            )
            association_result.participants_added_pending += (
                page_associations.participants_added_pending
            )
            association_result.participants_skipped_domain += (
                page_associations.participants_skipped_domain
            )
            association_result.email_deal_ids.update(page_associations.email_deal_ids)
            _merge_attachment_email_ids(
                association_result.attachment_email_ids_by_deal,
                page_associations.attachment_email_ids_by_deal,
            )

        # Perform one bounded expansion after all anchor pages are known. The nested
        # call disables discovery, so new contacts can add history but not another tier.
        if discover_participants and association_result.discovered_contact_ids:
            expansion = await self._backfill_contacts(
                sorted(association_result.discovered_contact_ids, key=str),
                integration_id=integration_id,
                discover_participants=False,
                target_deal_ids=target_deal_ids,
            )
            projected += expansion.activities_projected
            association_result.associations_created += expansion.deal_associations_created
            association_result.ambiguous += expansion.deal_associations_ambiguous
            association_result.discovered_contact_ids.update(expansion.discovered_contact_ids)
            association_result.email_deal_ids.update(expansion.email_deal_ids)
            _merge_attachment_email_ids(
                association_result.attachment_email_ids_by_deal,
                expansion.attachment_email_ids_by_deal,
            )
        return ContactCommunicationHydrationResult(
            emails_considered=emails_considered,
            events_considered=events_considered,
            contacts_created=len(created_contact_ids),
            contacts_reused=len(resolved_contact_ids - created_contact_ids),
            activities_projected=projected,
            skip_counters=dict(skip_counters),
            identities_considered=len(activity_identities),
            deal_associations_created=association_result.associations_created,
            deal_associations_ambiguous=association_result.ambiguous,
            deal_associations_deactivated=association_result.associations_deactivated,
            participants_auto_approved=association_result.participants_added_approved,
            participants_added_pending=association_result.participants_added_pending,
            participants_skipped_domain=association_result.participants_skipped_domain,
            discovered_contact_ids=frozenset(association_result.discovered_contact_ids),
            email_deal_ids=frozenset(association_result.email_deal_ids),
            attachment_email_ids_by_deal={
                deal_id: frozenset(email_ids)
                for deal_id, email_ids in association_result.attachment_email_ids_by_deal.items()
            },
        )

    async def backfill_deals(
        self,
        deal_ids: list[UUID],
        *,
        integration_id: UUID | None = None,
    ) -> ContactCommunicationHydrationResult:
        """Replay changed deals from their approved stakeholder contacts.

        Called after deal/stakeholder changes. It loads only approved contact
        anchors for the requested deals, hydrates their retained history, allows
        one bounded discovery pass, then reconciles old associations against the
        complete current-policy view.
        """
        contact_ids = await self._load_deal_contact_ids(deal_ids)
        result = await self._backfill_contacts(
            contact_ids,
            integration_id=integration_id,
            discover_participants=True,
            target_deal_ids=set(deal_ids),
        )
        # Reconcile after replay so every historical participant resolved above can
        # participate in the same current-policy and global-uniqueness decision.
        deactivated = await CommunicationDealAssociationService(
            self.org_session,
            self.organisation_id,
        ).reconcile_deal_associations(
            deal_ids=deal_ids,
            context=await self._load_context(),
        )
        if not deactivated:
            return result
        return replace(
            result,
            deal_associations_deactivated=(result.deal_associations_deactivated + deactivated),
        )

    async def _resolve_communication_participants(
        self,
        *,
        emails: list[Email],
        events: list[CalendarEvent],
        seed_contacts: list[CRMContact],
    ) -> CommunicationParticipantResolution:
        """Resolve every eligible identity before projection and deal association."""
        return await CommunicationParticipantResolutionService(
            self.org_session,
            self.master_session,
            self.organisation_id,
        ).resolve(
            emails=emails,
            events=events,
            context=await self._load_context(),
            seed_contacts=seed_contacts,
        )

    async def _write_stakeholder_candidates(
        self,
        association_result: CommunicationDealAssociationResult,
    ) -> None:
        """Write confident communication discoveries through the participant domain.

        The association service only gathers evidence. The stakeholder writer
        enforces customer-domain rules and the five-contact auto-approval cap;
        this method feeds only approved IDs into the single non-recursive expansion.
        """
        writer = StakeholderWriteService(self.org_session, self.organisation_id)
        for candidates in association_result.stakeholder_candidates:
            outcome = await writer.add_communication_discovered_contacts(
                deal_id=candidates.deal_id,
                integration_id=candidates.integration_id,
                customer_domains=candidates.customer_domains,
                contact_emails_by_id=candidates.contact_emails_by_id,
                source=candidates.source,
            )
            association_result.discovered_contact_ids.update(outcome.approved_contact_ids)
            association_result.participants_added_approved += outcome.approved_added
            association_result.participants_added_pending += outcome.pending_added
            association_result.participants_skipped_domain += outcome.skipped_domain

    async def _load_deal_contact_ids(self, deal_ids: list[UUID]) -> list[UUID]:
        """Load approved contact anchors for targeted deal creation/participant replay."""
        if not deal_ids:
            return []
        # A participant row is tenant-owned even though deal IDs are UUIDs; the org predicate
        # prevents a malformed worker payload from crossing schema ownership boundaries.
        result = await self.org_session.execute(
            select(col(CRMDealParticipant.related_local_id))
            .where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.deal_id).in_(deal_ids),
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
                col(CRMDealParticipant.is_deleted).is_(False),
                col(CRMDealParticipant.related_local_id).is_not(None),
            )
            .distinct()
        )
        return [contact_id for contact_id in result.scalars().all() if contact_id]

    async def _project(
        self,
        *,
        emails: list[Email],
        events: list[CalendarEvent],
        identity_contacts: dict[str, CRMContact],
        integration_id: UUID | None,
        replace_links: bool,
        target_deal_ids: set[UUID] | None,
        linked_email_ids: set[UUID] | None = None,
    ) -> int:
        """Project loaded communications onto resolved contacts and their companies.

        Existing deal associations own their visible activity cards. Otherwise a
        direct communication card is created, and a later association in this
        transaction can transfer ownership without duplicating the activity.
        """
        email_associations = await self._load_email_associations(emails)
        event_associations = await self._load_event_associations(events)
        association_ids = {
            association.association_id
            for associations in [*email_associations.values(), *event_associations.values()]
            for association in associations
        }
        active_activity_ids = (
            await self._load_active_activity_ids(association_ids)
            if target_deal_ids is not None
            else set()
        )
        projection = ActivityFeedProjectionService(
            self.org_session,
            self.organisation_id,
        )
        projected = 0
        projected_deal_ids: set[UUID] = set()

        # Existing association rows own deal-visible email cards; participant links
        # still include every eligible contact resolved from the source message.
        for email in emails:
            contacts = contacts_for_participants(email_participants(email), identity_contacts)
            if not contacts:
                continue
            contact_ids = {contact.contact_id for contact in contacts}
            company_ids = {contact.company_id for contact in contacts if contact.company_id}
            email_associations_for_record = email_associations.get(
                email.internet_message_id or "", []
            )
            if email_associations_for_record:
                for email_association in email_associations_for_record:
                    if (
                        target_deal_ids is None
                        or email_association.deal_id in target_deal_ids
                        or email_association.association_id not in active_activity_ids
                    ):
                        await projection.upsert_email_association(
                            email_association,
                            email,
                            refresh_deal_activity=False,
                        )
                        projected_deal_ids.add(email_association.deal_id)
                        projected += 1
                    await projection.sync_communication_participant_links(
                        activity_id=email_association.association_id,
                        contact_ids=contact_ids,
                        company_ids=company_ids,
                        occurred_at=(
                            email_association.email_date
                            or email.received_at
                            or email.sent_at
                            or datetime.now(UTC)
                        ),
                        replace_existing=replace_links,
                    )
                if linked_email_ids is not None:
                    linked_email_ids.add(email.email_id)
            else:
                if await projection.upsert_direct_email(
                    email,
                    contact_ids=contact_ids,
                    company_ids=company_ids,
                    replace_links=replace_links,
                ):
                    projected += 1
                if linked_email_ids is not None:
                    linked_email_ids.add(email.email_id)

        # Meetings mirror email projection, but future meetings do not advance
        # engagement recency until their scheduled time has passed.
        for event in events:
            contacts = contacts_for_participants(
                event_participants(event, Counter()), identity_contacts
            )
            if not contacts:
                continue
            contact_ids = {contact.contact_id for contact in contacts}
            company_ids = {contact.company_id for contact in contacts if contact.company_id}
            meeting_associations_for_event = event_associations.get(event.event_id, [])
            if meeting_associations_for_event:
                for meeting_association in meeting_associations_for_event:
                    if (
                        target_deal_ids is None
                        or meeting_association.deal_id in target_deal_ids
                        or meeting_association.association_id not in active_activity_ids
                    ):
                        await projection.upsert_meeting_association(
                            meeting_association,
                            event,
                            integration_id=integration_id,
                            refresh_deal_activity=False,
                        )
                        projected_deal_ids.add(meeting_association.deal_id)
                        projected += 1
                    now = datetime.now(UTC)
                    await projection.sync_communication_participant_links(
                        activity_id=meeting_association.association_id,
                        contact_ids=contact_ids,
                        company_ids=company_ids,
                        occurred_at=(
                            event.start_at if event.start_at and event.start_at <= now else None
                        )
                        or meeting_association.updated_at
                        or meeting_association.created_at
                        or now,
                        replace_existing=replace_links,
                    )
            else:
                if await projection.upsert_direct_meeting(
                    event,
                    integration_id=integration_id,
                    contact_ids=contact_ids,
                    company_ids=company_ids,
                    replace_links=replace_links,
                ):
                    projected += 1
        refresher = DealLastActivityService(self.org_session, self.organisation_id)
        for deal_id in sorted(projected_deal_ids, key=str):
            await refresher.refresh_deal(deal_id)
        return projected

    async def _load_active_activity_ids(self, activity_ids: set[UUID]) -> set[UUID]:
        """Return association-owned cards that participant links may safely reference."""
        if not activity_ids:
            return set()
        result = await self.org_session.execute(
            select(col(CRMActivity.activity_id)).where(
                col(CRMActivity.organisation_id) == self.organisation_id,
                col(CRMActivity.activity_id).in_(activity_ids),
                col(CRMActivity.is_deleted).is_(False),
            )
        )
        return set(result.scalars().all())

    async def _load_context(self) -> ContactCommunicationHydrationContext:
        """Load tenant-local people, domains, and integration mailboxes for eligibility checks."""
        internal_emails: set[str] = set()
        internal_domains: set[str] = set()
        users = await self.master_session.execute(
            select(col(User.email)).where(col(User.organisation_id) == self.organisation_id)
        )
        for raw_user_email in users.scalars().all():
            if user_email := normalize_email(str(raw_user_email)):
                internal_emails.add(user_email)

        organisation = await self.master_session.execute(
            select(col(Organisation.domain)).where(
                col(Organisation.organisation_id) == self.organisation_id
            )
        )
        raw_domains = organisation.scalar_one_or_none()
        if isinstance(raw_domains, list):
            internal_domains.update(normalize_internal_domains(raw_domains))

        # Integration mailboxes are internal identities. The organisation predicate is the
        # tenant boundary; provider-owned email addresses alone are never trusted for scoping.
        integrations = await self.org_session.execute(
            select(col(KnowledgeIntegration.integration_email)).where(
                col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                col(KnowledgeIntegration.integration_email).is_not(None),
            )
        )
        for raw_integration_email in integrations.scalars().all():
            if integration_email := normalize_email(str(raw_integration_email)):
                internal_emails.add(integration_email)

        return ContactCommunicationHydrationContext(
            internal_emails=internal_emails,
            internal_domains={
                domain for domain in internal_domains if domain not in FREE_EMAIL_DOMAINS
            },
        )

    async def _load_changed_emails(self, email_ids: list[UUID]) -> list[Email]:
        """Load active email rows named by the current queue payload under this organisation."""
        if not email_ids:
            return []
        # Changed IDs arrive through Celery and are untrusted without the organisation filter.
        # Pair both predicates so a task can never hydrate another tenant's provider rows.
        result = await self.org_session.execute(
            select(Email).where(
                col(Email.organisation_id) == self.organisation_id,
                col(Email.email_id).in_(email_ids),
                col(Email.is_deleted).is_(False),
            )
        )
        return list(result.scalars().all())

    async def _load_email_thread_context(self, changed_emails: list[Email]) -> list[Email]:
        """Load retained rows in the changed messages' provider threads.

        Gmail and Outlook both persist provider thread IDs. Messages without one remain
        single-message conversations; subject text is deliberately ignored because unrelated
        cold emails often reuse the same subject.
        """
        if not changed_emails:
            return []
        thread_keys = {
            (email.integration_id, email.thread_id) for email in changed_emails if email.thread_id
        }
        related = await self._load_email_context_rows(thread_keys)
        return list({email.email_id: email for email in [*changed_emails, *related]}.values())

    async def _load_email_context_rows(
        self,
        thread_keys: set[tuple[UUID, str]],
    ) -> list[Email]:
        """Load active org-scoped rows matching provider thread identities."""
        if not thread_keys:
            return []
        # Thread IDs come from provider payloads; organisation scope remains the trust boundary
        # preventing one tenant's identifiers from selecting another's rows.
        result = await self.org_session.execute(
            select(Email).where(
                col(Email.organisation_id) == self.organisation_id,
                col(Email.is_deleted).is_(False),
                tuple_(col(Email.integration_id), col(Email.thread_id)).in_(thread_keys),
            )
        )
        return list(result.scalars().all())

    async def _load_changed_events(self, event_ids: list[UUID]) -> list[CalendarEvent]:
        """Load calendar rows named by the current queue payload under this organisation."""
        if not event_ids:
            return []
        # Event IDs also come from Celery payloads; the organisation predicate is the trust
        # boundary that prevents one tenant's IDs from selecting another tenant's events.
        result = await self.org_session.execute(
            select(CalendarEvent)
            .options(selectinload(cast(Any, CalendarEvent.attendees)))
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.event_id).in_(event_ids),
            )
        )
        return list(result.scalars().unique().all())

    async def _load_contacts(self, contact_ids: list[UUID]) -> list[CRMContact]:
        """Load active local contacts requested for an explicit activity-history backfill."""
        if not contact_ids:
            return []
        # Contact IDs come from API/tool payloads; organisation scoping remains the authorization
        # boundary even though each tenant normally has its own schema.
        result = await self.org_session.execute(
            select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.contact_id).in_(contact_ids),
                col(CRMContact.is_deleted).is_(False),
            )
        )
        return list(result.scalars().all())

    async def _load_emails_for_identities(
        self,
        identities: set[str],
        *,
        offset: int = 0,
        limit: int = HISTORY_PAGE_SIZE,
    ) -> list[Email]:
        """Page retained org emails involving any approved anchor identity.

        JSON participant arrays are queried directly so history hydration stays
        source-driven and never falls back to an organisation-wide mapper.
        """
        if not identities:
            return []
        identity_array = array(sorted(identities))
        participant_filters = [
            col(column).op("?|")(identity_array)
            for column in (Email.to_emails, Email.cc_emails, Email.bcc_emails)
        ]
        result = await self.org_session.execute(
            select(Email)
            .where(
                col(Email.organisation_id) == self.organisation_id,
                col(Email.is_deleted).is_(False),
                or_(
                    func.lower(func.trim(col(Email.from_email))).in_(identities),
                    *participant_filters,
                ),
            )
            .order_by(col(Email.email_id))
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def _load_events_for_identities(
        self,
        identities: set[str],
        *,
        offset: int = 0,
        limit: int = HISTORY_PAGE_SIZE,
    ) -> list[CalendarEvent]:
        """Page retained org events involving any approved anchor identity.

        Organizer and attendee storage differ, so both sources are queried while
        the outer event predicate preserves the tenant boundary.
        """
        if not identities:
            return []
        attendee_events = select(col(CalendarAttendee.event_id)).where(
            func.lower(func.trim(col(CalendarAttendee.email))).in_(identities)
        )
        result = await self.org_session.execute(
            select(CalendarEvent)
            .options(selectinload(cast(Any, CalendarEvent.attendees)))
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                or_(
                    func.lower(func.trim(col(CalendarEvent.organiser_email))).in_(identities),
                    col(CalendarEvent.event_id).in_(attendee_events),
                ),
            )
            .order_by(col(CalendarEvent.event_id))
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def _load_email_associations(
        self,
        emails: list[Email],
    ) -> dict[str, list[DealEmailAssociation]]:
        """Group active deal-email associations by provider message ID for card ownership."""
        message_ids = {email.internet_message_id for email in emails if email.internet_message_id}
        if not message_ids:
            return {}
        statement = select(DealEmailAssociation).where(
            col(DealEmailAssociation.organisation_id) == self.organisation_id,
            col(DealEmailAssociation.internet_message_id).in_(message_ids),
            col(DealEmailAssociation.status) == AssociationStatus.ACTIVE.value,
        )
        result = await self.org_session.execute(statement)
        grouped: dict[str, list[DealEmailAssociation]] = {}
        for association in result.scalars().all():
            grouped.setdefault(association.internet_message_id, []).append(association)
        return grouped

    async def _load_event_associations(
        self,
        events: list[CalendarEvent],
    ) -> dict[UUID, list[CalendarEventDealAssociation]]:
        """Group active deal-meeting associations by event ID for card ownership."""
        event_ids = {event.event_id for event in events}
        if not event_ids:
            return {}
        statement = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            col(CalendarEventDealAssociation.event_id).in_(event_ids),
            col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE.value,
        )
        result = await self.org_session.execute(statement)
        grouped: dict[UUID, list[CalendarEventDealAssociation]] = {}
        for association in result.scalars().all():
            grouped.setdefault(association.event_id, []).append(association)
        return grouped
