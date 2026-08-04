"""Resolve communication participants into canonical CRM contacts."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarAttendee,
    CalendarEvent,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.email_models import Email
from src.service.crm.contacts.contracts import ContactCreateCommand, ContactWriteContext
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.utils.identity import extract_email_domain, is_valid_email, normalize_email

# Large meetings are commonly webinars or company-wide events; skip automatic contact creation
# above this product guardrail rather than flooding CRM with weak attendee identities.
MAX_CONTACT_DISCOVERY_ATTENDEES = 25
_ROLE_MAILBOX_LOCAL_PARTS = {
    "accounts",
    "admin",
    "alerts",
    "apply",
    "billing",
    "bot",
    "calendar",
    "careers",
    "compliance",
    "contact",
    "donotreply",
    "events",
    "finance",
    "hello",
    "help",
    "hr",
    "info",
    "insider",
    "jobs",
    "legal",
    "mailerdaemon",
    "marketing",
    "newsletter",
    "noreply",
    "notification",
    "notifications",
    "partners",
    "postmaster",
    "privacy",
    "recruiting",
    "resource",
    "room",
    "sales",
    "security",
    "success",
    "support",
    "team",
    "webinar",
}


class CommunicationHydrationBusyError(RuntimeError):
    """Signal that another transaction currently owns this organisation's hydration."""


class SkipReason(StrEnum):
    """Explain why a communication participant was not eligible for CRM creation."""

    INTERNAL_EMAIL = "internal_email"
    INTERNAL_DOMAIN = "internal_domain"
    MALFORMED_EMAIL = "malformed_email"
    ROLE_MAILBOX = "role_mailbox"
    DECLINED_ATTENDEE = "declined_attendee"
    RESOURCE_ATTENDEE = "resource_attendee"
    NO_INTERNAL_PARTICIPANT = "no_internal_participant"
    INBOUND_UNENGAGED = "inbound_unengaged"
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
    """Activity identities and creation candidates for one communication batch."""

    candidates: list[ContactCommunicationCandidate]
    activity_identities: set[str]
    skip_counters: dict[str, int]
    email_contact_identities: dict[UUID, frozenset[str]]
    event_contact_identities: dict[UUID, frozenset[str]]


@dataclass(frozen=True)
class CommunicationParticipantResolution:
    """Contacts and metrics resolved from one bounded communication batch."""

    plan: ContactCommunicationHydrationPlan
    identity_contacts: dict[str, CRMContact]
    created_contact_ids: frozenset[UUID]
    reused_contact_ids: frozenset[UUID]
    created_account_ids: frozenset[UUID] = field(default_factory=frozenset)


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
    email_contact_identities: dict[UUID, frozenset[str]] = {}
    event_contact_identities: dict[UUID, frozenset[str]] = {}

    # A sent message is evidence that a coworker intentionally engaged the thread. Inbound-only
    # threads remain available for activity matching but cannot turn cold outreach into contacts.
    for thread_emails in _group_email_threads(emails):
        participants = [
            participant
            for email_record in thread_emails
            for participant in email_participants(email_record)
        ]
        if not _has_internal_participant(participants, context):
            counters[SkipReason.NO_INTERNAL_PARTICIPANT.value] += 1
            continue
        create_contacts = any(
            _is_internal_identity(email_record.from_email, context)
            for email_record in thread_emails
        )
        if not create_contacts:
            counters[SkipReason.INBOUND_UNENGAGED.value] += 1
        for email_record in thread_emails:
            email_contact_identities[email_record.email_id] = _eligible_contact_identities(
                email_participants(email_record),
                context,
            )
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

    # Bound attendee fan-out before identity work so webinar-sized events cannot create
    # hundreds of contacts. Existing contacts still receive the meeting activity.
    for event in events:
        create_contacts = len(event.attendees or []) <= MAX_CONTACT_DISCOVERY_ATTENDEES
        if not create_contacts:
            counters[SkipReason.TOO_MANY_ATTENDEES.value] += 1
        participants = event_participants(event, counters)
        if not _has_internal_participant(participants, context):
            counters[SkipReason.NO_INTERNAL_PARTICIPANT.value] += 1
            continue
        event_contact_identities[event.event_id] = _eligible_contact_identities(
            participants,
            context,
        )
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
        email_contact_identities=email_contact_identities,
        event_contact_identities=event_contact_identities,
    )


class CommunicationParticipantResolutionService:
    """Apply one participant-resolution policy to every hydration mode."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
    ) -> None:
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id

    async def resolve(
        self,
        *,
        emails: list[Email],
        events: list[CalendarEvent],
        context: ContactCommunicationHydrationContext,
        seed_contacts: list[CRMContact],
    ) -> CommunicationParticipantResolution:
        """Resolve every eligible identity before projection and deal association."""
        plan = build_contact_communication_hydration_plan(
            emails=emails,
            events=events,
            context=context,
        )
        if plan.candidates:
            # copilotgtm: serialize contact identity creation per org until a
            # unique normalized-email constraint can replace this global lock.
            # Do not occupy a worker while another hydration owns the transaction;
            # the Celery boundary retries this bounded batch.
            lock_result = await self.org_session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"contact-communication-hydration:{self.organisation_id}"},
            )
            if not lock_result.scalar_one():
                raise CommunicationHydrationBusyError(
                    f"Contact communication hydration already in progress for "
                    f"organisation {self.organisation_id}"
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
        identity_contacts = identity_contact_map(seed_contacts)
        identity_contacts.update({email: result.contact for email, result in resolved.items()})
        unresolved_activity_identities = plan.activity_identities - set(identity_contacts)
        identity_contacts.update(
            await writer.find_contacts_by_email_batch(sorted(unresolved_activity_identities))
        )
        created_contact_ids = frozenset(
            result.contact.contact_id for result in resolved.values() if result.created
        )
        return CommunicationParticipantResolution(
            plan=plan,
            identity_contacts=identity_contacts,
            created_contact_ids=created_contact_ids,
            reused_contact_ids=frozenset(
                contact.contact_id for contact in identity_contacts.values()
            )
            - created_contact_ids,
            created_account_ids=frozenset(
                result.created_account_id
                for result in resolved.values()
                if result.created_account_id is not None
            ),
        )


def email_participants(email: Email) -> list[tuple[str | None, str | None]]:
    """Return sender and recipient identities from a locally persisted email row."""
    return [
        (email.from_email, email.from_name),
        *[(value, None) for value in email.to_emails or []],
        *[(value, None) for value in email.cc_emails or []],
        *[(value, None) for value in email.bcc_emails or []],
    ]


def event_participants(
    event: CalendarEvent,
    counters: Counter[str],
) -> list[tuple[str | None, str | None]]:
    """Return eligible identities while counting declined and resource attendee exclusions."""
    participants: list[tuple[str | None, str | None]] = [
        (event.organiser_email, event.organiser_name)
    ]
    for attendee in event.attendees or []:
        if str(attendee.response_status or "").lower() == "declined":
            counters[SkipReason.DECLINED_ATTENDEE.value] += 1
        elif _is_resource_attendee(attendee):
            counters[SkipReason.RESOURCE_ATTENDEE.value] += 1
        else:
            participants.append((attendee.email, attendee.display_name))
    return participants


def identity_contact_map(contacts: list[CRMContact]) -> dict[str, CRMContact]:
    """Index explicit contacts by primary and additional normalized email addresses."""
    result: dict[str, CRMContact] = {}
    for contact in contacts:
        for raw_email in [contact.email, *(contact.additional_emails or [])]:
            if email := normalize_email(raw_email):
                result[email] = contact
    return result


def contacts_for_participants(
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


def _is_role_mailbox(email: str) -> bool:
    local_part = email.rsplit("@", 1)[0].split("+", 1)[0]
    compact = re.sub(r"[^a-z0-9]", "", local_part)
    tokens = set(re.findall(r"[a-z0-9]+", local_part))
    return compact in _ROLE_MAILBOX_LOCAL_PARTS or bool(tokens & _ROLE_MAILBOX_LOCAL_PARTS)


def _usable_display_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = " ".join(name.strip().split())
    if not cleaned or "@" in cleaned:
        return None
    return cleaned


def _group_email_threads(emails: list[Email]) -> list[list[Email]]:
    """Group retained messages by provider thread without guessing from subject text."""
    grouped: dict[tuple[str, UUID, str], list[Email]] = {}
    for email in emails:
        key = (
            ("thread", email.integration_id, email.thread_id)
            if email.thread_id
            else ("message", email.integration_id, str(email.email_id))
        )
        grouped.setdefault(key, []).append(email)
    return list(grouped.values())


def _is_internal_identity(
    raw_email: str | None,
    context: ContactCommunicationHydrationContext,
) -> bool:
    normalized = normalize_email(raw_email)
    return bool(
        normalized
        and is_valid_email(normalized)
        and (
            normalized in context.internal_emails
            or extract_email_domain(normalized) in context.internal_domains
        )
    )


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
    return any(_is_internal_identity(raw_email, context) for raw_email, _name in participants)


def _eligible_contact_identities(
    participants: list[tuple[str | None, str | None]],
    context: ContactCommunicationHydrationContext,
) -> frozenset[str]:
    return frozenset(
        normalized
        for raw_email, _name in participants
        if (normalized := normalize_email(raw_email))
        and is_valid_email(normalized)
        and normalized not in context.internal_emails
        and extract_email_domain(normalized) not in context.internal_domains
    )


def _is_resource_attendee(attendee: CalendarAttendee) -> bool:
    payload = attendee.data_payload
    return isinstance(payload, dict) and (
        payload.get("resource") is True or str(payload.get("type", "")).lower() == "resource"
    )
