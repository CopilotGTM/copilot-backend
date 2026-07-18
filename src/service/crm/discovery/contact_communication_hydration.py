"""Create contacts from committed communications and backfill contact activity links.

Integration sync calls the changed-source path; manual API and agent-tool contact
creation call the explicit-contact path. CSV import remains outside this flow.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import text
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
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation, Email
from src.service.crm.activity_feed_projection_service import (
    ActivityFeedProjectionService,
)
from src.service.crm.contacts.contracts import ContactCreateCommand, ContactWriteContext
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.guards.internal_user_guard import FREE_EMAIL_DOMAINS
from src.service.crm.utils.identity import extract_email_domain, is_valid_email, normalize_email
from src.service.crm.utils.internal_domains import normalize_internal_domains

# Large meetings are commonly webinars or company-wide events; skip automatic contact creation
# above this product guardrail rather than flooding CRM with weak attendee identities.
MAX_CONTACT_DISCOVERY_ATTENDEES = 25
_ROLE_MAILBOX_LOCAL_PARTS = {
    "donotreply",
    "mailer-daemon",
    "no-reply",
    "noreply",
    "postmaster",
}


class SkipReason(StrEnum):
    """Explain why a communication participant was not eligible for CRM creation."""

    INTERNAL_EMAIL = "internal_email"
    INTERNAL_DOMAIN = "internal_domain"
    MALFORMED_EMAIL = "malformed_email"
    ROLE_MAILBOX = "role_mailbox"
    DECLINED_ATTENDEE = "declined_attendee"
    RESOURCE_ATTENDEE = "resource_attendee"
    NO_INTERNAL_PARTICIPANT = "no_internal_participant"
    TOO_MANY_ATTENDEES = "too_many_attendees"


@dataclass(frozen=True)
class ContactCommunicationHydrationContext:
    """Tenant-local identities used to distinguish coworkers from CRM contacts."""

    internal_emails: set[str]
    internal_domains: set[str]


@dataclass(frozen=True)
class ContactCommunicationCandidate:
    """One external identity eligible for automatic CRM contact creation."""

    email: str
    name: str | None


@dataclass(frozen=True)
class ContactCommunicationHydrationPlan:
    """Activity identities, creation candidates, and filtering counters for one batch."""

    candidates: list[ContactCommunicationCandidate]
    activity_identities: set[str]
    skip_counters: dict[str, int]


@dataclass(frozen=True)
class ContactCommunicationHydrationResult:
    """Compact worker result suitable for logs and Celery task responses."""

    emails_considered: int
    events_considered: int
    contacts_created: int
    contacts_reused: int
    activities_projected: int
    skip_counters: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result without leaking ORM rows into Celery."""
        return {
            "emails_considered": self.emails_considered,
            "events_considered": self.events_considered,
            "contacts_created": self.contacts_created,
            "contacts_reused": self.contacts_reused,
            "activities_projected": self.activities_projected,
            "skip_counters": self.skip_counters,
        }


def build_contact_communication_hydration_plan(
    *,
    emails: list[Email],
    events: list[CalendarEvent],
    context: ContactCommunicationHydrationContext,
) -> ContactCommunicationHydrationPlan:
    """Extract and deduplicate external human participants from local source rows."""
    counters: Counter[str] = Counter()
    candidates: dict[str, str | None] = {}
    activity_identities: set[str] = set()

    # A communication is eligible only when a known coworker participated; otherwise an
    # arbitrary provider row could create contacts unrelated to this organisation.
    for email_record in emails:
        participants = _email_participants(email_record)
        if not _has_internal_participant(participants, context):
            counters[SkipReason.NO_INTERNAL_PARTICIPANT.value] += 1
            continue
        for raw_email, name in participants:
            _add_candidate(
                candidates,
                counters,
                activity_identities,
                raw_email=raw_email,
                name=name,
                context=context,
                create_contact=True,
            )

    # Bound attendee fan-out before identity work so webinar-sized events cannot create
    # hundreds of contacts. Existing contacts still receive the meeting activity.
    for event in events:
        create_contacts = len(event.attendees or []) <= MAX_CONTACT_DISCOVERY_ATTENDEES
        if not create_contacts:
            counters[SkipReason.TOO_MANY_ATTENDEES.value] += 1
        participants = _event_participants(event, counters)
        if not _has_internal_participant(participants, context):
            counters[SkipReason.NO_INTERNAL_PARTICIPANT.value] += 1
            continue
        for raw_email, name in participants:
            _add_candidate(
                candidates,
                counters,
                activity_identities,
                raw_email=raw_email,
                name=name,
                context=context,
                create_contact=create_contacts,
            )

    return ContactCommunicationHydrationPlan(
        candidates=[
            ContactCommunicationCandidate(
                email=email,
                name=candidates[email],
            )
            for email in sorted(candidates)
        ],
        activity_identities=activity_identities,
        skip_counters=dict(counters),
    )


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
        """Create contacts from one committed email/calendar change batch."""
        emails = await self._load_changed_emails(changed_email_ids or [])
        events = await self._load_changed_events(changed_event_ids or [])
        plan = build_contact_communication_hydration_plan(
            emails=emails,
            events=events,
            context=await self._load_context(),
        )
        if plan.candidates:
            # copilotgtm: serialize contact identity creation per org until a
            # unique normalized-email constraint can replace this global lock.
            await self.org_session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"contact-communication-hydration:{self.organisation_id}"},
            )
        writer = ContactWriteService(
            self.org_session,
            self.organisation_id,
            self.master_session,
        )
        resolved = await writer.find_or_create_contacts_by_email_batch(
            [
                ContactCreateCommand(email=candidate.email, name=candidate.name)
                for candidate in plan.candidates
            ],
            context=ContactWriteContext(
                source="contact_communication_hydration",
                created_via="contact_communication_hydration",
            ),
        )
        identity_contacts = {email: result.contact for email, result in resolved.items()}
        # Creation guardrails must not hide history from contacts already in the CRM. Resolve
        # the remaining activity identities without creating a contact when no match exists.
        unresolved_activity_identities = plan.activity_identities - set(identity_contacts)
        identity_contacts.update(
            await writer.find_contacts_by_email_batch(sorted(unresolved_activity_identities))
        )
        projected = await self._project(
            emails=emails,
            events=events,
            identity_contacts=identity_contacts,
            integration_id=integration_id,
            replace_links=True,
        )
        created_contact_ids = {
            result.contact.contact_id for result in resolved.values() if result.created
        }
        reused_contact_ids = {
            contact.contact_id for contact in identity_contacts.values()
        } - created_contact_ids
        return ContactCommunicationHydrationResult(
            emails_considered=len(emails),
            events_considered=len(events),
            contacts_created=len(created_contact_ids),
            contacts_reused=len(reused_contact_ids),
            activities_projected=projected,
            skip_counters=plan.skip_counters,
        )

    async def backfill_contacts(
        self,
        contact_ids: list[UUID],
        *,
        integration_id: UUID | None = None,
    ) -> ContactCommunicationHydrationResult:
        """Attach retained communication history to already-created contacts."""
        contacts = await self._load_contacts(contact_ids)
        identity_contacts = _identity_contact_map(contacts)
        identities = set(identity_contacts)
        emails = await self._load_emails_for_identities(identities)
        events = await self._load_events_for_identities(identities)

        projected = await self._project(
            emails=emails,
            events=events,
            identity_contacts=identity_contacts,
            integration_id=integration_id,
            replace_links=False,
        )
        return ContactCommunicationHydrationResult(
            emails_considered=len(emails),
            events_considered=len(events),
            contacts_created=0,
            contacts_reused=len(contacts),
            activities_projected=projected,
            skip_counters={},
        )

    async def _project(
        self,
        *,
        emails: list[Email],
        events: list[CalendarEvent],
        identity_contacts: dict[str, CRMContact],
        integration_id: UUID | None,
        replace_links: bool,
    ) -> int:
        """Project loaded communications onto resolved contacts and their companies."""
        email_associations = await self._load_email_associations(emails)
        event_associations = await self._load_event_associations(events)
        projection = ActivityFeedProjectionService(
            self.org_session,
            self.organisation_id,
        )
        projected = 0

        for email in emails:
            contacts = _contacts_for_participants(_email_participants(email), identity_contacts)
            if not contacts:
                continue
            contact_ids = {contact.contact_id for contact in contacts}
            company_ids = {contact.company_id for contact in contacts if contact.company_id}
            email_associations_for_record = email_associations.get(
                email.internet_message_id or "", []
            )
            if email_associations_for_record:
                for email_association in email_associations_for_record:
                    await projection.upsert_email_association(email_association, email)
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
                    projected += 1
            else:
                if await projection.upsert_direct_email(
                    email,
                    contact_ids=contact_ids,
                    company_ids=company_ids,
                    replace_links=replace_links,
                ):
                    projected += 1

        for event in events:
            contacts = _contacts_for_participants(
                _event_participants(event, Counter()), identity_contacts
            )
            if not contacts:
                continue
            contact_ids = {contact.contact_id for contact in contacts}
            company_ids = {contact.company_id for contact in contacts if contact.company_id}
            meeting_associations_for_event = event_associations.get(event.event_id, [])
            if meeting_associations_for_event:
                for meeting_association in meeting_associations_for_event:
                    await projection.upsert_meeting_association(
                        meeting_association,
                        event,
                        integration_id=integration_id,
                    )
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
                    projected += 1
            else:
                if await projection.upsert_direct_meeting(
                    event,
                    integration_id=integration_id,
                    contact_ids=contact_ids,
                    company_ids=company_ids,
                    replace_links=replace_links,
                ):
                    projected += 1
        return projected

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

    async def _load_emails_for_identities(self, identities: set[str]) -> list[Email]:
        """Find retained local emails involving any identity from explicit contacts."""
        if not identities:
            return []
        identity_array = array(sorted(identities))
        participant_filters = [
            col(column).op("?|")(identity_array)
            for column in (Email.to_emails, Email.cc_emails, Email.bcc_emails)
        ]
        result = await self.org_session.execute(
            select(Email).where(
                col(Email.organisation_id) == self.organisation_id,
                col(Email.is_deleted).is_(False),
                or_(
                    func.lower(func.trim(col(Email.from_email))).in_(identities),
                    *participant_filters,
                ),
            )
        )
        return list(result.scalars().all())

    async def _load_events_for_identities(self, identities: set[str]) -> list[CalendarEvent]:
        """Find retained calendar events involving any identity from explicit contacts."""
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
        result = await self.org_session.execute(
            select(DealEmailAssociation).where(
                col(DealEmailAssociation.organisation_id) == self.organisation_id,
                col(DealEmailAssociation.internet_message_id).in_(message_ids),
                col(DealEmailAssociation.status) == AssociationStatus.ACTIVE.value,
            )
        )
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
        result = await self.org_session.execute(
            select(CalendarEventDealAssociation).where(
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                col(CalendarEventDealAssociation.event_id).in_(event_ids),
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE.value,
            )
        )
        grouped: dict[UUID, list[CalendarEventDealAssociation]] = {}
        for association in result.scalars().all():
            grouped.setdefault(association.event_id, []).append(association)
        return grouped


def _is_role_mailbox(email: str) -> bool:
    local_part = email.rsplit("@", 1)[0]
    compact = re.sub(r"[^a-z0-9]", "", local_part)
    return local_part in _ROLE_MAILBOX_LOCAL_PARTS or compact in _ROLE_MAILBOX_LOCAL_PARTS


def _usable_display_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = " ".join(name.strip().split())
    if not cleaned or "@" in cleaned:
        return None
    return cleaned


def _email_participants(email: Email) -> list[tuple[str | None, str | None]]:
    """Return sender and recipient identities from a locally persisted email row."""
    return [
        (email.from_email, email.from_name),
        *[(value, None) for value in email.to_emails or []],
        *[(value, None) for value in email.cc_emails or []],
        *[(value, None) for value in email.bcc_emails or []],
    ]


def _event_participants(
    event: CalendarEvent,
    counters: Counter[str],
) -> list[tuple[str | None, str | None]]:
    """Return eligible identities while counting declined and resource attendee exclusions."""
    participants: list[tuple[str | None, str | None]] = [
        (event.organiser_email, event.organiser_name)
    ]
    for attendee in event.attendees or []:
        # Providers retain declined invitees and room/resource attendees in event payloads;
        # neither represents a useful human relationship to materialize as a CRM contact.
        if str(attendee.response_status or "").lower() == "declined":
            counters[SkipReason.DECLINED_ATTENDEE.value] += 1
        elif _is_resource_attendee(attendee.data_payload):
            counters[SkipReason.RESOURCE_ATTENDEE.value] += 1
        else:
            participants.append((attendee.email, attendee.display_name))
    return participants


def _add_candidate(
    candidates: dict[str, str | None],
    counters: Counter[str],
    activity_identities: set[str],
    *,
    raw_email: str | None,
    name: str | None,
    context: ContactCommunicationHydrationContext,
    create_contact: bool,
) -> None:
    """Collect an external activity identity, then optionally allow automatic creation."""
    normalized = normalize_email(raw_email)
    domain = extract_email_domain(normalized)
    if not normalized or not is_valid_email(normalized):
        counters[SkipReason.MALFORMED_EMAIL.value] += 1
    elif normalized in context.internal_emails:
        counters[SkipReason.INTERNAL_EMAIL.value] += 1
    elif domain in context.internal_domains:
        counters[SkipReason.INTERNAL_DOMAIN.value] += 1
    else:
        activity_identities.add(normalized)
        if not create_contact:
            return
        if _is_role_mailbox(normalized):
            counters[SkipReason.ROLE_MAILBOX.value] += 1
            return
        usable_name = _usable_display_name(name)
        if normalized not in candidates or candidates[normalized] is None:
            candidates[normalized] = usable_name


def _has_internal_participant(
    participants: list[tuple[str | None, str | None]],
    context: ContactCommunicationHydrationContext,
) -> bool:
    """Require one known internal participant before a communication can create contacts."""
    return any(
        normalized in context.internal_emails
        or extract_email_domain(normalized) in context.internal_domains
        for raw_email, _name in participants
        if (normalized := normalize_email(raw_email)) and is_valid_email(normalized)
    )


def _is_resource_attendee(payload: dict | None) -> bool:
    """Recognize provider room/resource attendee payloads that are not people."""
    return isinstance(payload, dict) and (
        payload.get("resource") is True or str(payload.get("type", "")).lower() == "resource"
    )


def _identity_contact_map(contacts: list[CRMContact]) -> dict[str, CRMContact]:
    """Index explicit contacts by primary and additional normalized email addresses."""
    result: dict[str, CRMContact] = {}
    for contact in contacts:
        for raw_email in [contact.email, *(contact.additional_emails or [])]:
            if email := normalize_email(raw_email):
                result[email] = contact
    return result


def _contacts_for_participants(
    participants: list[tuple[str | None, str | None]],
    identity_contacts: dict[str, CRMContact],
) -> list[CRMContact]:
    """Deduplicate contacts whose known identities appear in a source participant list."""
    contacts: dict[UUID, CRMContact] = {}
    for raw_email, _name in participants:
        contact = identity_contacts.get(normalize_email(raw_email) or "")
        if contact:
            contacts[contact.contact_id] = contact
    return list(contacts.values())
