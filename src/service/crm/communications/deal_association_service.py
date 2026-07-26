"""Associate hydrated communications with eligible CRM deals.

Contact communication hydration calls this module after it resolves activity
participants. The pure resolver keeps relationship activity on contact/account
feeds unless both a deal contact and a current internal deal participant are
present. Emails and calendar events may belong to every deal satisfying that
rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm import (
    ApprovalStatus,
    CRMDeal,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.stakeholders import StakeholderSource
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    DealEmailDirection,
    Email,
)
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.communications.participant_resolution_service import (
    ContactCommunicationHydrationContext,
    build_contact_communication_hydration_plan,
)
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.utils.account import normalize_account_domain
from src.service.crm.utils.identity import extract_email_domain, normalize_email
from src.service.integrations.email.mailbox_identity_resolver import (
    EMAIL_MATCHER_INTEGRATION_TYPES,
    get_active_integration_emails_grouped_by_user,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CommunicationCandidate:
    """Minimal communication evidence needed to choose a deal safely."""

    contact_ids: frozenset[UUID]
    participant_emails: frozenset[str]
    contact_emails: frozenset[str] = field(default_factory=frozenset)
    existing_deal_ids: frozenset[UUID] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DealCandidate:
    """Open deal identities compared with one hydrated communication."""

    deal_id: UUID
    contact_ids: frozenset[UUID]
    internal_emails: frozenset[str]
    integration_id: UUID | None = None
    customer_domains: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AdditionalStakeholderCandidates:
    """Participant-domain write request derived from a confident communication match."""

    deal_id: UUID
    integration_id: UUID
    customer_domains: frozenset[str]
    contact_emails_by_id: dict[UUID, str]
    source: StakeholderSource


@dataclass
class CommunicationDealAssociationResult:
    """Counts emitted by one bounded hydration association pass."""

    considered: int = 0
    associations_created: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    associations_deactivated: int = 0
    discovered_contact_ids: set[UUID] = field(default_factory=set)
    stakeholder_candidates: list[AdditionalStakeholderCandidates] = field(default_factory=list)
    participants_added_approved: int = 0
    participants_added_pending: int = 0
    participants_skipped_domain: int = 0
    email_deal_ids: set[UUID] = field(default_factory=set)
    attachment_email_ids_by_deal: dict[UUID, set[UUID]] = field(default_factory=dict)


def resolve_unique_deal_id(
    communication: CommunicationCandidate,
    deals: list[DealCandidate],
) -> UUID | None:
    """Return the sole current rule match; historical rows never establish eligibility."""
    matching_deal_ids = {
        deal.deal_id for deal in deals if communication_matches_deal(communication, deal)
    }
    return next(iter(matching_deal_ids)) if len(matching_deal_ids) == 1 else None


def communication_matches_deal(
    communication: CommunicationCandidate,
    deal: DealCandidate,
) -> bool:
    """Require both an approved deal contact and a current internal participant."""
    return bool(
        communication.contact_ids.intersection(deal.contact_ids)
        and communication.participant_emails.intersection(deal.internal_emails)
    )


class CommunicationDealAssociationService:
    """Map hydrated email/calendar records without starting an organisation scan."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def associate_changed_communications(
        self,
        *,
        emails: list[Email],
        events: list[CalendarEvent],
        identity_contacts: dict[str, CRMContact],
        integration_id: UUID | None,
        attachment_scope_email_ids: set[UUID],
        email_contact_identities: dict[UUID, frozenset[str]] | None = None,
        event_contact_identities: dict[UUID, frozenset[str]] | None = None,
        discover_participants: bool = True,
        target_deal_ids: set[UUID] | None = None,
    ) -> CommunicationDealAssociationResult:
        """Associate one hydration batch without scanning unrelated communications."""
        _ = integration_id
        cancelled_event_ids = {
            event.event_id for event in events if str(event.status or "").lower() == "cancelled"
        }
        cancelled_count = await self._cancel_event_associations(
            cancelled_event_ids,
            deal_ids=target_deal_ids,
        )
        email_deals, event_deals = await self._load_existing_deal_ids(emails, events)
        prepared_emails = [
            (
                email,
                self._candidate_for_participants(
                    self._email_participant_emails(email),
                    identity_contacts,
                    frozenset(email_deals.get(email.internet_message_id or "", set())),
                    contact_identity_emails=(
                        email_contact_identities.get(email.email_id, frozenset())
                        if email_contact_identities is not None
                        else None
                    ),
                ),
            )
            for email in emails
        ]
        prepared_events = [
            (
                event,
                self._candidate_for_participants(
                    self._event_participant_emails(event),
                    identity_contacts,
                    frozenset(event_deals.get(event.event_id, set())),
                    contact_identity_emails=(
                        event_contact_identities.get(event.event_id, frozenset())
                        if event_contact_identities is not None
                        else None
                    ),
                ),
            )
            for event in events
        ]
        contact_ids = {
            contact_id
            for _, communication in [*prepared_emails, *prepared_events]
            for contact_id in communication.contact_ids
        }
        # Resolve against every eligible deal before applying an optional replay target.
        deals = await self._load_deal_candidates(contact_ids)
        deals_by_id = {deal.deal_id: deal for deal in deals}
        result = CommunicationDealAssociationResult(
            considered=len(emails) + len(events),
            associations_deactivated=cancelled_count,
        )
        pending_discovery: dict[
            tuple[UUID, StakeholderSource],
            dict[UUID, set[str]],
        ] = {}

        def remember_discovery(
            deal_id: UUID,
            source: StakeholderSource,
            communication: CommunicationCandidate,
        ) -> None:
            """Retain identities that supplied evidence for this communication match."""
            contact_emails = pending_discovery.setdefault((deal_id, source), {})
            for email in communication.contact_emails:
                if contact := identity_contacts.get(email):
                    contact_emails.setdefault(contact.contact_id, set()).add(email)

        for email, communication in prepared_emails:
            deal_ids = {
                deal.deal_id for deal in deals if communication_matches_deal(communication, deal)
            }
            if target_deal_ids is not None:
                deal_ids.intersection_update(target_deal_ids)
            if not deal_ids:
                result.unmatched += 1
                continue
            for deal_id in sorted(deal_ids, key=str):
                result.email_deal_ids.add(deal_id)
                # The model is typed as bool but backed by a string column. Accept only the two
                # canonical values that the extractor's SQL predicate can process.
                attachment_flag = getattr(email, "has_attachments", False)
                if email.email_id in attachment_scope_email_ids and (
                    attachment_flag is True or attachment_flag == "true"
                ):
                    result.attachment_email_ids_by_deal.setdefault(deal_id, set()).add(
                        email.email_id
                    )
                if deal_id in communication.existing_deal_ids:
                    await self._update_last_contact(
                        deal_id=deal_id,
                        contact_ids=set(communication.contact_ids),
                        contact_time=email.received_at or email.sent_at,
                    )
                    if discover_participants and deal_id in deals_by_id:
                        remember_discovery(
                            deal_id,
                            StakeholderSource.EMAIL_DISCOVERY,
                            communication,
                        )
                    continue
                if await self._associate_email(
                    email,
                    deal_id=deal_id,
                    contact_ids=set(communication.contact_ids),
                    contact_emails=set(communication.contact_emails),
                ):
                    result.associations_created += 1
                    if discover_participants:
                        remember_discovery(
                            deal_id,
                            StakeholderSource.EMAIL_DISCOVERY,
                            communication,
                        )

        for event, communication in prepared_events:
            if str(event.status or "").lower() == "cancelled":
                result.unmatched += 1
                continue
            # TODO(copilotgtm): Meetings mirror email matching so replay works with parallel
            # deals. Decide whether shared meetings should instead require explicit ownership.
            deal_ids = {
                deal.deal_id for deal in deals if communication_matches_deal(communication, deal)
            }
            if target_deal_ids is not None:
                deal_ids.intersection_update(target_deal_ids)
            if not deal_ids:
                result.unmatched += 1
                continue
            for deal_id in sorted(deal_ids, key=str):
                if deal_id in communication.existing_deal_ids:
                    await self._update_last_contact(
                        deal_id=deal_id,
                        contact_ids=set(communication.contact_ids),
                        contact_time=self._completed_event_time(event),
                    )
                    if discover_participants and deal_id in deals_by_id:
                        remember_discovery(
                            deal_id,
                            StakeholderSource.CALENDAR_DISCOVERY,
                            communication,
                        )
                    continue
                if await self._associate_event(
                    event,
                    deal_id=deal_id,
                    contact_ids=set(communication.contact_ids),
                    contact_emails=set(communication.contact_emails),
                    integration_id=integration_id,
                ):
                    result.associations_created += 1
                    if discover_participants:
                        remember_discovery(
                            deal_id,
                            StakeholderSource.CALENDAR_DISCOVERY,
                            communication,
                        )

        for (deal_id, source), discovery_contact_emails in pending_discovery.items():
            deal = deals_by_id[deal_id]
            if deal.integration_id is None:
                continue
            result.stakeholder_candidates.append(
                AdditionalStakeholderCandidates(
                    deal_id=deal_id,
                    integration_id=deal.integration_id,
                    customer_domains=deal.customer_domains,
                    contact_emails_by_id={
                        contact_id: min(
                            emails,
                            key=lambda email: (
                                extract_email_domain(email) not in deal.customer_domains,
                                email,
                            ),
                        )
                        for contact_id, emails in discovery_contact_emails.items()
                    },
                    source=source,
                )
            )

        return result

    async def reconcile_deal_associations(
        self,
        *,
        deal_ids: list[UUID],
        context: ContactCommunicationHydrationContext,
    ) -> int:
        """Remove active links that no longer satisfy current participant policy."""
        if not deal_ids:
            return 0
        email_rows, event_rows = await self._load_associated_communications(deal_ids)
        emails = [email for _association, email in email_rows]
        events = [event for _association, event in event_rows]
        plan = build_contact_communication_hydration_plan(
            emails=emails,
            events=events,
            context=context,
        )
        identity_contacts = await ContactWriteService(
            self.session,
            self.organisation_id,
        ).find_contacts_by_email_batch(sorted(plan.activity_identities))
        contact_ids = {contact.contact_id for contact in identity_contacts.values()}
        deals = await self._load_deal_candidates(contact_ids)
        deals_by_id = {deal.deal_id: deal for deal in deals}
        projection = ActivityFeedProjectionService(self.session, self.organisation_id)
        removed = 0

        for email_association, email in email_rows:
            communication = self._candidate_for_participants(
                self._email_participant_emails(email),
                identity_contacts,
                frozenset(),
                contact_identity_emails=plan.email_contact_identities.get(
                    email.email_id,
                    frozenset(),
                ),
            )
            deal = deals_by_id.get(email_association.deal_id)
            if deal and communication_matches_deal(communication, deal):
                continue
            email_association.status = AssociationStatus.REMOVED
            email_association.updated_at = datetime.now(UTC)
            await projection.soft_delete_activity_projection(email_association.association_id)
            removed += 1

        for event_association, event in event_rows:
            communication = self._candidate_for_participants(
                self._event_participant_emails(event),
                identity_contacts,
                frozenset(),
                contact_identity_emails=plan.event_contact_identities.get(
                    event.event_id,
                    frozenset(),
                ),
            )
            deal = deals_by_id.get(event_association.deal_id)
            if str(event.status or "").lower() == "cancelled":
                event_association.status = AssociationStatus.CANCELLED
            elif deal and communication_matches_deal(communication, deal):
                continue
            else:
                event_association.status = AssociationStatus.REMOVED
            event_association.updated_at = datetime.now(UTC)
            await projection.soft_delete_activity_projection(event_association.association_id)
            removed += 1
        return removed

    async def _load_associated_communications(
        self,
        deal_ids: list[UUID],
    ) -> tuple[
        list[tuple[DealEmailAssociation, Email]],
        list[tuple[CalendarEventDealAssociation, CalendarEvent]],
    ]:
        """Load only active source rows already linked to the requested deals."""
        email_result = await self.session.execute(
            select(DealEmailAssociation, Email)
            .join(
                Email,
                (col(Email.organisation_id) == col(DealEmailAssociation.organisation_id))
                & (col(Email.internet_message_id) == col(DealEmailAssociation.internet_message_id)),
            )
            .where(
                col(DealEmailAssociation.organisation_id) == self.organisation_id,
                col(DealEmailAssociation.deal_id).in_(deal_ids),
                col(DealEmailAssociation.status) == AssociationStatus.ACTIVE.value,
                col(Email.is_deleted).is_(False),
            )
        )
        email_by_association = {
            association.association_id: (association, email)
            for association, email in email_result.all()
        }

        event_rel = cast(Any, CalendarEventDealAssociation.event)
        attendees_rel = cast(Any, CalendarEvent.attendees)
        event_result = await self.session.execute(
            select(CalendarEventDealAssociation)
            .options(selectinload(event_rel).selectinload(attendees_rel))
            .where(
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                col(CalendarEventDealAssociation.deal_id).in_(deal_ids),
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE.value,
            )
        )
        event_associations = list(event_result.scalars().unique().all())
        return list(email_by_association.values()), [
            (association, association.event)
            for association in event_associations
            if association.event is not None
        ]

    async def _cancel_event_associations(
        self,
        event_ids: set[UUID],
        *,
        deal_ids: set[UUID] | None,
    ) -> int:
        """Cancel active meeting links, restricted to replayed deals when supplied."""
        if not event_ids:
            return 0
        # Provider event IDs are not a tenant boundary, and targeted replay must not
        # mutate the same meeting's association on an unrelated deal.
        statement = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            col(CalendarEventDealAssociation.event_id).in_(event_ids),
            col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE.value,
        )
        if deal_ids is not None:
            statement = statement.where(col(CalendarEventDealAssociation.deal_id).in_(deal_ids))
        rows = await self.session.execute(statement)
        associations = list(rows.scalars().all())
        projection = ActivityFeedProjectionService(self.session, self.organisation_id)
        for association in associations:
            association.status = AssociationStatus.CANCELLED
            association.updated_at = datetime.now(UTC)
            await projection.soft_delete_activity_projection(association.association_id)
        return len(associations)

    def _candidate_for_participants(
        self,
        participant_emails: set[str],
        identity_contacts: dict[str, CRMContact],
        existing_deal_ids: frozenset[UUID],
        *,
        contact_identity_emails: frozenset[str] | None = None,
    ) -> CommunicationCandidate:
        normalized_emails = {email for raw in participant_emails if (email := normalize_email(raw))}
        eligible_contact_emails = (
            normalized_emails
            if contact_identity_emails is None
            else {email for raw in contact_identity_emails if (email := normalize_email(raw))}
        )
        return CommunicationCandidate(
            contact_ids=frozenset(
                identity_contacts[email].contact_id
                for email in eligible_contact_emails
                if email in identity_contacts
            ),
            participant_emails=frozenset(normalized_emails),
            contact_emails=frozenset(
                email for email in eligible_contact_emails if email in identity_contacts
            ),
            existing_deal_ids=existing_deal_ids,
        )

    @staticmethod
    def _count_unresolved(
        result: CommunicationDealAssociationResult,
        communication: CommunicationCandidate,
        deals: list[DealCandidate],
    ) -> None:
        matches = [deal for deal in deals if communication_matches_deal(communication, deal)]
        if len(matches) > 1:
            result.ambiguous += 1
        else:
            result.unmatched += 1

    def _email_participant_emails(self, email: Email) -> set[str]:
        return {
            value
            for value in [
                email.from_email,
                *(email.to_emails or []),
                *(email.cc_emails or []),
                *(email.bcc_emails or []),
            ]
            if value
        }

    def _event_participant_emails(self, event: CalendarEvent) -> set[str]:
        return {
            value
            for value in [
                event.organiser_email,
                *[attendee.email for attendee in event.attendees or []],
            ]
            if value
        }

    async def _load_existing_deal_ids(
        self,
        emails: list[Email],
        events: list[CalendarEvent],
    ) -> tuple[dict[str, set[UUID]], dict[UUID, set[UUID]]]:
        email_deals: dict[str, set[UUID]] = {}
        event_deals: dict[UUID, set[UUID]] = {}
        message_ids = {email.internet_message_id for email in emails if email.internet_message_id}
        if message_ids:
            # The org predicate is the tenant boundary; a provider message ID is not trusted
            # to be globally unique across connected customer mailboxes.
            email_rows = await self.session.execute(
                select(DealEmailAssociation).where(
                    col(DealEmailAssociation.organisation_id) == self.organisation_id,
                    col(DealEmailAssociation.internet_message_id).in_(message_ids),
                    col(DealEmailAssociation.status) == AssociationStatus.ACTIVE.value,
                )
            )
            for email_association in email_rows.scalars().all():
                email_deals.setdefault(email_association.internet_message_id, set()).add(
                    email_association.deal_id
                )

        event_ids = {event.event_id for event in events}
        if event_ids:
            event_rows = await self.session.execute(
                select(CalendarEventDealAssociation).where(
                    col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                    col(CalendarEventDealAssociation.event_id).in_(event_ids),
                    col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE.value,
                )
            )
            for event_association in event_rows.scalars().all():
                event_deals.setdefault(event_association.event_id, set()).add(
                    event_association.deal_id
                )
        return email_deals, event_deals

    async def _load_deal_candidates(
        self,
        contact_ids: set[UUID],
    ) -> list[DealCandidate]:
        if not contact_ids:
            return []
        participants_rel = cast(Any, CRMDeal.participants)
        # Start from approved contact links so a PENDING participant discovered by this flow
        # cannot recursively widen the same deal's historical communication graph.
        stmt = (
            select(CRMDeal)
            .join(
                CRMDealParticipant,
                col(CRMDealParticipant.deal_id) == col(CRMDeal.deal_id),
            )
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_status) == CRMDealStatus.OPEN,
                col(CRMDeal.is_deleted).is_(False),
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
                col(CRMDealParticipant.related_local_id).in_(contact_ids),
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
                col(CRMDealParticipant.is_deleted).is_(False),
            )
            .options(selectinload(participants_rel))
            .distinct()
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        user_ids_by_deal: dict[UUID, set[UUID]] = {}
        for deal in rows:
            user_ids = {
                participant.related_local_id
                for participant in deal.participants or []
                if participant.participant_type == CRMParticipantType.USER
                and not participant.is_deleted
                and participant.related_local_id
            }
            if deal.owner_id:
                user_ids.add(deal.owner_id)
            user_ids_by_deal[deal.deal_id] = user_ids
        integration_emails_by_user = await get_active_integration_emails_grouped_by_user(
            session=self.session,
            organisation_id=self.organisation_id,
            created_by_user_ids={
                user_id for user_ids in user_ids_by_deal.values() for user_id in user_ids
            },
            integration_types=EMAIL_MATCHER_INTEGRATION_TYPES,
        )
        candidates: list[DealCandidate] = []
        for deal in rows:
            approved_contact_ids = {
                participant.related_local_id
                for participant in deal.participants or []
                if participant.participant_type == CRMParticipantType.CONTACT
                and not participant.is_deleted
                and participant.approval_status == ApprovalStatus.APPROVED.value
                and participant.related_local_id in contact_ids
            }
            internal_emails = {
                email
                for raw_email in [
                    deal.owner_email,
                    *[
                        participant.related_email
                        for participant in deal.participants or []
                        if participant.participant_type == CRMParticipantType.USER
                        and not participant.is_deleted
                    ],
                ]
                if (email := normalize_email(raw_email))
            }
            internal_emails.update(
                email
                for user_id in user_ids_by_deal[deal.deal_id]
                for email in integration_emails_by_user.get(user_id, set())
            )
            customer_domains = {
                domain
                for participant in deal.participants or []
                if participant.participant_type == CRMParticipantType.CONTACT
                and not participant.is_deleted
                and participant.approval_status == ApprovalStatus.APPROVED.value
                and (domain := extract_email_domain(participant.related_email))
            }
            if account_domain := normalize_account_domain(deal.company_domain):
                customer_domains.add(account_domain)
            if approved_contact_ids and internal_emails:
                candidates.append(
                    DealCandidate(
                        deal_id=deal.deal_id,
                        integration_id=deal.integration_id,
                        contact_ids=frozenset(approved_contact_ids),
                        internal_emails=frozenset(internal_emails),
                        customer_domains=frozenset(customer_domains),
                    )
                )
        return candidates

    async def _associate_email(
        self,
        email: Email,
        *,
        deal_id: UUID,
        contact_ids: set[UUID],
        contact_emails: set[str],
    ) -> bool:
        _ = contact_ids
        if not email.internet_message_id:
            return False
        from_email = normalize_email(email.from_email)
        association = DealEmailAssociation(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            internet_message_id=email.internet_message_id,
            status=AssociationStatus.ACTIVE,
            match_score=1.0,
            direction=(
                DealEmailDirection.INBOUND
                if from_email in contact_emails
                else DealEmailDirection.OUTBOUND
            ),
            matched_stakeholder_emails=sorted(contact_emails),
            email_subject=email.subject[:500] if email.subject else None,
            email_date=email.received_at or email.sent_at,
        )
        # The source unique constraint is the retry boundary. Only the first insert may
        # transfer the visible card from direct hydration to association ownership.
        insert_stmt = pg_insert(DealEmailAssociation).values(
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
        result = await self.session.execute(
            insert_stmt.on_conflict_do_update(
                constraint="uq_deal_email",
                set_={
                    "status": AssociationStatus.ACTIVE.value,
                    "match_score": insert_stmt.excluded.match_score,
                    "direction": insert_stmt.excluded.direction,
                    "matched_stakeholder_emails": (insert_stmt.excluded.matched_stakeholder_emails),
                    "email_subject": insert_stmt.excluded.email_subject,
                    "email_date": insert_stmt.excluded.email_date,
                    "updated_at": insert_stmt.excluded.updated_at,
                },
                where=col(DealEmailAssociation.status).in_(
                    [
                        AssociationStatus.REMOVED.value,
                        AssociationStatus.CANCELLED.value,
                    ]
                ),
            ).returning(DealEmailAssociation.association_id)
        )
        persisted_association_id = result.scalar_one_or_none()
        if persisted_association_id is None:
            return False
        association.association_id = persisted_association_id
        await ActivityFeedProjectionService(
            self.session,
            self.organisation_id,
        ).upsert_email_association(association, email)
        await self._update_last_contact(
            deal_id=deal_id,
            contact_ids=contact_ids,
            contact_time=email.received_at or email.sent_at,
        )
        return True

    async def _associate_event(
        self,
        event: CalendarEvent,
        *,
        deal_id: UUID,
        contact_ids: set[UUID],
        contact_emails: set[str],
        integration_id: UUID | None,
    ) -> bool:
        _ = contact_ids, contact_emails
        association = CalendarEventDealAssociation(
            organisation_id=self.organisation_id,
            event_id=event.event_id,
            deal_id=deal_id,
            status=AssociationStatus.ACTIVE,
            match_score=1.0,
        )
        insert_stmt = pg_insert(CalendarEventDealAssociation).values(
            association_id=association.association_id,
            organisation_id=association.organisation_id,
            event_id=association.event_id,
            deal_id=association.deal_id,
            status=association.status,
            match_score=association.match_score,
            meeting_prep_status=association.meeting_prep_status,
            meeting_summary_status=association.meeting_summary_status,
            meeting_followup_status=association.meeting_followup_status,
            created_at=association.created_at,
            updated_at=association.updated_at,
        )
        result = await self.session.execute(
            insert_stmt.on_conflict_do_update(
                constraint="uq_calendar_event_deal",
                set_={
                    "status": AssociationStatus.ACTIVE.value,
                    "match_score": insert_stmt.excluded.match_score,
                    "updated_at": insert_stmt.excluded.updated_at,
                },
                where=col(CalendarEventDealAssociation.status).in_(
                    [
                        AssociationStatus.REMOVED.value,
                        AssociationStatus.CANCELLED.value,
                    ]
                ),
            ).returning(CalendarEventDealAssociation.association_id)
        )
        persisted_association_id = result.scalar_one_or_none()
        if persisted_association_id is None:
            return False
        association.association_id = persisted_association_id
        await ActivityFeedProjectionService(
            self.session,
            self.organisation_id,
        ).upsert_meeting_association(
            association,
            event,
            integration_id=integration_id,
        )
        contact_time = self._completed_event_time(event)
        await self._update_last_contact(
            deal_id=deal_id,
            contact_ids=contact_ids,
            contact_time=contact_time,
        )
        return True

    @staticmethod
    def _completed_event_time(event: CalendarEvent) -> datetime | None:
        """Return a normalized meeting time only after the meeting has ended."""
        contact_time = event.end_at or event.start_at
        if contact_time is None:
            return None
        normalized_time = (
            contact_time.replace(tzinfo=UTC)
            if contact_time.tzinfo is None
            else contact_time.astimezone(UTC)
        )
        return normalized_time if normalized_time <= datetime.now(UTC) else None

    async def _update_last_contact(
        self,
        *,
        deal_id: UUID,
        contact_ids: set[UUID],
        contact_time: datetime | None,
    ) -> None:
        """Advance engagement recency only for approved contacts in this communication."""
        if not contact_ids or contact_time is None:
            return
        rows = await self.session.execute(
            select(CRMDealParticipant).where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.deal_id) == deal_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
                col(CRMDealParticipant.related_local_id).in_(contact_ids),
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
                col(CRMDealParticipant.is_deleted).is_(False),
            )
        )
        now = datetime.now(UTC)
        for participant in rows.scalars().all():
            if participant.last_contact_at is None or contact_time > participant.last_contact_at:
                participant.last_contact_at = contact_time
                participant.updated_at = now
