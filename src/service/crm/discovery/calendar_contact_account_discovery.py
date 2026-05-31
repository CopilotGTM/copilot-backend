from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.sync import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventUserState,
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.guards.internal_user_guard import FREE_EMAIL_DOMAINS

DISCOVERY_SOURCE = "calendar_initial_discovery"
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
MAX_DISCOVERY_ATTENDEES_PER_EVENT = 25

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,63}$")
_LOCAL_SPLIT_RE = re.compile(r"[._+\-]+")
_PLACEHOLDER_NAMES = {"unknown", "none", "n/a", "na", "user", "guest", "attendee"}
_ROLE_MAILBOX_LOCAL_PARTS = {
    "admin",
    "billing",
    "bot",
    "calendar",
    "careers",
    "donotreply",
    "events",
    "help",
    "info",
    "jobs",
    "legal",
    "mailer-daemon",
    "marketing",
    "newsletter",
    "no-reply",
    "noreply",
    "notification",
    "postmaster",
    "privacy",
    "recruiting",
    "resource",
    "room",
    "sales",
    "security",
    "support",
    "webinar",
}


class SkipReason(str, Enum):
    CANCELLED_EVENT = "cancelled_event"
    ALL_DAY_EVENT = "all_day_event"
    NO_ATTENDEES = "no_attendees"
    TOO_MANY_ATTENDEES = "too_many_attendees"
    NO_INTERNAL_PARTICIPANT = "no_internal_participant"
    MALFORMED_EMAIL = "malformed_email"
    ROLE_MAILBOX = "role_mailbox"
    FREE_EMAIL_DOMAIN = "free_email_domain"
    INTERNAL_EMAIL = "internal_email"
    INTERNAL_DOMAIN = "internal_domain"
    DECLINED = "declined"
    ORGANIZER = "organizer"
    RESOURCE_ATTENDEE = "resource_attendee"
    MISSING_NAME = "missing_name"
    EXISTING_CONTACT = "existing_contact"
    EXISTING_CONTACT_NEWER_THAN_EVENT = "existing_contact_newer_than_event"
    EXISTING_ACCOUNT = "existing_account"


@dataclass(frozen=True)
class CalendarDiscoveryAttendee:
    email: str
    display_name: str | None
    response_status: str
    is_organiser: bool
    is_resource: bool = False


@dataclass(frozen=True)
class CalendarDiscoveryEvent:
    event_id: UUID
    start_at: datetime
    user_state_updated_at: datetime
    status: str | None
    is_all_day: bool
    attendees: list[CalendarDiscoveryAttendee]


@dataclass(frozen=True)
class CalendarDiscoveryContext:
    organisation_id: UUID
    source_integration_id: UUID
    internal_emails: set[str]
    internal_domains: set[str]


@dataclass(frozen=True)
class AccountCandidate:
    domain: str
    name: str
    properties: dict[str, Any]


@dataclass(frozen=True)
class ContactCandidate:
    email: str
    first_name: str | None
    last_name: str | None
    full_name: str
    company_domain: str
    properties: dict[str, Any]


@dataclass(frozen=True)
class DiscoveryPlan:
    accounts: list[AccountCandidate]
    contacts: list[ContactCandidate]
    skip_counters: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarDiscoveryResult:
    events_considered: int
    min_event_user_state_updated_at: datetime | None
    max_event_user_state_updated_at: datetime | None
    accounts_created: int
    contacts_created: int
    accounts_skipped: int
    contacts_skipped: int
    skip_counters: dict[str, int]


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    match = _EMAIL_RE.match(normalized)
    if not match:
        return None
    domain = _email_domain(normalized)
    if not _domain_labels_are_valid(domain):
        return None
    return normalized


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1]


def _domain_labels_are_valid(domain: str) -> bool:
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    return all(label and not label.startswith("-") and not label.endswith("-") for label in labels)


def _is_role_mailbox(email: str) -> bool:
    local_part = email.rsplit("@", 1)[0]
    compact = re.sub(r"[^a-z0-9]", "", local_part)
    return local_part in _ROLE_MAILBOX_LOCAL_PARTS or compact in _ROLE_MAILBOX_LOCAL_PARTS


def _usable_display_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        return None
    if cleaned.lower() in _PLACEHOLDER_NAMES:
        return None
    if "@" in cleaned:
        return None
    return cleaned


def _derive_name_from_email(email: str) -> str | None:
    local_part = email.rsplit("@", 1)[0].split("+", 1)[0]
    parts = [part for part in _LOCAL_SPLIT_RE.split(local_part) if part.isalpha()]
    if len(parts) < 2:
        return None
    return " ".join(part.capitalize() for part in parts)


def _split_name(name: str) -> tuple[str | None, str | None]:
    parts = name.split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _account_name_from_domain(domain: str) -> str:
    label = domain.split(".", 1)[0].replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in label.split()) or domain


def _event_is_eligible(event: CalendarDiscoveryEvent, counters: Counter[str]) -> bool:
    if (event.status or "").lower() == "cancelled":
        counters[SkipReason.CANCELLED_EVENT.value] += 1
        return False
    if event.is_all_day:
        counters[SkipReason.ALL_DAY_EVENT.value] += 1
        return False
    if not event.attendees:
        counters[SkipReason.NO_ATTENDEES.value] += 1
        return False
    if len(event.attendees) > MAX_DISCOVERY_ATTENDEES_PER_EVENT:
        counters[SkipReason.TOO_MANY_ATTENDEES.value] += 1
        return False
    return True


def _attendee_is_internal(
    attendee: CalendarDiscoveryAttendee,
    context: CalendarDiscoveryContext,
) -> bool:
    email = _normalize_email(attendee.email)
    if not email:
        return False
    return email in context.internal_emails or _email_domain(email) in context.internal_domains


def _candidate_skip_reason(
    attendee: CalendarDiscoveryAttendee,
    context: CalendarDiscoveryContext,
) -> SkipReason | None:
    email = _normalize_email(attendee.email)
    if not email:
        return SkipReason.MALFORMED_EMAIL
    domain = _email_domain(email)
    if attendee.is_organiser:
        return SkipReason.ORGANIZER
    if attendee.is_resource:
        return SkipReason.RESOURCE_ATTENDEE
    if attendee.response_status.lower() == "declined":
        return SkipReason.DECLINED
    if _is_role_mailbox(email):
        return SkipReason.ROLE_MAILBOX
    if domain in FREE_EMAIL_DOMAINS:
        return SkipReason.FREE_EMAIL_DOMAIN
    if email in context.internal_emails:
        return SkipReason.INTERNAL_EMAIL
    if domain in context.internal_domains:
        return SkipReason.INTERNAL_DOMAIN
    if not (_usable_display_name(attendee.display_name) or _derive_name_from_email(email)):
        return SkipReason.MISSING_NAME
    return None


def _candidate_properties(
    *,
    context: CalendarDiscoveryContext,
    evidence_event_ids: set[UUID],
    first_seen_at: datetime,
    last_seen_at: datetime,
    skip_counters: Counter[str],
) -> dict[str, Any]:
    return {
        "created_via": DISCOVERY_SOURCE,
        "source_integration_id": str(context.source_integration_id),
        "evidence_event_ids": sorted(str(event_id) for event_id in evidence_event_ids),
        "first_seen_at": first_seen_at.isoformat(),
        "last_seen_at": last_seen_at.isoformat(),
        "skip_counters": dict(skip_counters),
    }


def build_discovery_plan(
    *,
    events: list[CalendarDiscoveryEvent],
    context: CalendarDiscoveryContext,
    existing_contacts: dict[str, datetime | None],
    existing_company_domains: set[str],
) -> DiscoveryPlan:
    counters: Counter[str] = Counter()
    evidence_by_email: dict[str, set[UUID]] = {}
    seen_at_by_email: dict[str, tuple[datetime, datetime]] = {}
    names_by_email: dict[str, str] = {}

    for event in events:
        if not _event_is_eligible(event, counters):
            continue
        if not any(_attendee_is_internal(attendee, context) for attendee in event.attendees):
            counters[SkipReason.NO_INTERNAL_PARTICIPANT.value] += 1
            continue

        for attendee in event.attendees:
            skip_reason = _candidate_skip_reason(attendee, context)
            if skip_reason:
                counters[skip_reason.value] += 1
                continue

            email = _normalize_email(attendee.email)
            if email is None:
                counters[SkipReason.MALFORMED_EMAIL.value] += 1
                continue
            domain = _email_domain(email)
            existing_contact_updated_at = existing_contacts.get(email)
            if email in existing_contacts:
                if existing_contact_updated_at and existing_contact_updated_at > event.start_at:
                    counters[SkipReason.EXISTING_CONTACT_NEWER_THAN_EVENT.value] += 1
                else:
                    counters[SkipReason.EXISTING_CONTACT.value] += 1
                continue
            if domain in existing_company_domains:
                counters[SkipReason.EXISTING_ACCOUNT.value] += 1
                continue

            evidence_by_email.setdefault(email, set()).add(event.event_id)
            first_seen, last_seen = seen_at_by_email.get(email, (event.start_at, event.start_at))
            seen_at_by_email[email] = (
                min(first_seen, event.start_at),
                max(last_seen, event.start_at),
            )
            names_by_email[email] = (
                _usable_display_name(attendee.display_name) or _derive_name_from_email(email) or ""
            )

    domains = sorted({_email_domain(email) for email in evidence_by_email})
    accounts: list[AccountCandidate] = []
    for domain in domains:
        domain_events = {
            event_id
            for email, event_ids in evidence_by_email.items()
            if _email_domain(email) == domain
            for event_id in event_ids
        }
        domain_seen = [
            seen for email, seen in seen_at_by_email.items() if _email_domain(email) == domain
        ]
        first_seen_at = min(seen[0] for seen in domain_seen)
        last_seen_at = max(seen[1] for seen in domain_seen)
        accounts.append(
            AccountCandidate(
                domain=domain,
                name=_account_name_from_domain(domain),
                properties=_candidate_properties(
                    context=context,
                    evidence_event_ids=domain_events,
                    first_seen_at=first_seen_at,
                    last_seen_at=last_seen_at,
                    skip_counters=counters,
                ),
            )
        )

    contacts: list[ContactCandidate] = []
    for email in sorted(evidence_by_email):
        full_name = names_by_email[email]
        first_name, last_name = _split_name(full_name)
        first_seen_at, last_seen_at = seen_at_by_email[email]
        contacts.append(
            ContactCandidate(
                email=email,
                first_name=first_name,
                last_name=last_name,
                full_name=full_name,
                company_domain=_email_domain(email),
                properties=_candidate_properties(
                    context=context,
                    evidence_event_ids=evidence_by_email[email],
                    first_seen_at=first_seen_at,
                    last_seen_at=last_seen_at,
                    skip_counters=counters,
                ),
            )
        )

    return DiscoveryPlan(
        accounts=accounts,
        contacts=contacts,
        skip_counters=dict(counters),
    )


class CalendarContactAccountDiscoveryService:
    def __init__(
        self,
        *,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
        source_integration_id: UUID,
        event_user_state_updated_after: datetime | None = None,
    ) -> None:
        self.org_session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.source_integration_id = source_integration_id
        self.event_user_state_updated_after = event_user_state_updated_after

    async def run(self) -> CalendarDiscoveryResult:
        await self._acquire_org_discovery_lock()
        context = await self._load_context()
        existing_contacts = await self._load_existing_contacts()
        existing_company_domains = await self._load_existing_company_domains()
        events = await self._load_source_calendar_events()
        plan = build_discovery_plan(
            events=events,
            context=context,
            existing_contacts=existing_contacts,
            existing_company_domains=existing_company_domains,
        )

        internal_integration = await self._get_or_create_internal_integration()
        created_companies = await self._create_accounts(plan.accounts, internal_integration)
        created_contacts = await self._create_contacts(
            plan.contacts,
            internal_integration,
            created_companies,
        )
        user_state_updated_values = [event.user_state_updated_at for event in events]
        return CalendarDiscoveryResult(
            events_considered=len(events),
            min_event_user_state_updated_at=(
                min(user_state_updated_values) if user_state_updated_values else None
            ),
            max_event_user_state_updated_at=(
                max(user_state_updated_values) if user_state_updated_values else None
            ),
            accounts_created=len(created_companies),
            contacts_created=created_contacts,
            accounts_skipped=len(plan.accounts) - len(created_companies),
            contacts_skipped=len(plan.contacts) - created_contacts,
            skip_counters=plan.skip_counters,
        )

    async def _load_context(self) -> CalendarDiscoveryContext:
        internal_emails: set[str] = set()
        internal_domains: set[str] = set()

        users_stmt = select(col(User.email)).where(
            col(User.organisation_id) == self.organisation_id
        )
        users_result = await self.master_session.execute(users_stmt)
        for email in users_result.scalars().all():
            normalized = _normalize_email(str(email))
            if normalized:
                internal_emails.add(normalized)
                internal_domains.add(_email_domain(normalized))

        org_stmt = select(col(Organisation.domain)).where(
            col(Organisation.organisation_id) == self.organisation_id
        )
        org_result = await self.master_session.execute(org_stmt)
        domains_raw = org_result.scalar_one_or_none()
        if isinstance(domains_raw, list):
            for domain in domains_raw:
                normalized = self._normalize_domain(str(domain))
                if normalized:
                    internal_domains.add(normalized)

        integration_stmt = select(col(KnowledgeIntegration.integration_email)).where(
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            col(KnowledgeIntegration.integration_email).is_not(None),
        )
        integration_result = await self.org_session.execute(integration_stmt)
        for email in integration_result.scalars().all():
            normalized = _normalize_email(str(email))
            if normalized:
                internal_emails.add(normalized)
                internal_domains.add(_email_domain(normalized))

        internal_domains = {
            domain for domain in internal_domains if domain not in FREE_EMAIL_DOMAINS
        }
        return CalendarDiscoveryContext(
            organisation_id=self.organisation_id,
            source_integration_id=self.source_integration_id,
            internal_emails=internal_emails,
            internal_domains=internal_domains,
        )

    async def _acquire_org_discovery_lock(self) -> None:
        await self.org_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"{DISCOVERY_SOURCE}:{self.organisation_id}"},
        )

    async def _load_existing_contacts(self) -> dict[str, datetime | None]:
        stmt = select(func.lower(col(CRMContact.email)), col(CRMContact.updated_at)).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            col(CRMContact.email).is_not(None),
        )
        result = await self.org_session.execute(stmt)
        return {str(email): updated_at for email, updated_at in result.all() if email}

    async def _load_existing_company_domains(self) -> set[str]:
        stmt = select(func.lower(col(CRMCompany.domain))).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted).is_(False),
            col(CRMCompany.domain).is_not(None),
        )
        result = await self.org_session.execute(stmt)
        return {str(domain) for domain in result.scalars().all() if domain}

    async def _load_source_calendar_events(self) -> list[CalendarDiscoveryEvent]:
        stmt = (
            select(CalendarEvent, CalendarEventUserState, CalendarAttendee)
            .join(CalendarEventUserState, CalendarEventUserState.event_id == CalendarEvent.event_id)
            .join(CalendarAttendee, CalendarAttendee.event_id == CalendarEvent.event_id)
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.is_deleted).is_(False),
                col(CalendarEventUserState.integration_id) == self.source_integration_id,
                col(CalendarEventUserState.is_deleted).is_(False),
            )
            .order_by(CalendarEvent.start_at.asc())
        )
        if self.event_user_state_updated_after is not None:
            stmt = stmt.where(
                col(CalendarEventUserState.updated_at) > self.event_user_state_updated_after
            )
        result = await self.org_session.execute(stmt)
        events_by_id: dict[UUID, CalendarDiscoveryEvent] = {}
        for event, user_state, attendee in result.all():
            discovery_event = events_by_id.get(event.event_id)
            if discovery_event is None:
                discovery_event = CalendarDiscoveryEvent(
                    event_id=event.event_id,
                    start_at=event.start_at,
                    user_state_updated_at=user_state.updated_at,
                    status=event.status,
                    is_all_day=event.is_all_day,
                    attendees=[],
                )
                events_by_id[event.event_id] = discovery_event
            elif user_state.updated_at > discovery_event.user_state_updated_at:
                object.__setattr__(
                    discovery_event,
                    "user_state_updated_at",
                    user_state.updated_at,
                )
            discovery_event.attendees.append(
                CalendarDiscoveryAttendee(
                    email=attendee.email,
                    display_name=attendee.display_name,
                    response_status=attendee.response_status,
                    is_organiser=attendee.is_organiser,
                    is_resource=self._is_resource_attendee(attendee.data_payload),
                )
            )
        return list(events_by_id.values())

    async def _get_or_create_internal_integration(self) -> KnowledgeIntegration:
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            col(KnowledgeIntegration.integration_type) == IntegrationType.INTERNAL_CRM.value,
        )
        result = await self.org_session.execute(stmt)
        integration = result.scalars().first()
        if integration:
            return integration

        integration = KnowledgeIntegration(
            organisation_id=self.organisation_id,
            created_by_user_id=AUTOMATED_PIPELINE_USER_ID,
            created_by_admin=False,
            integration_type=IntegrationType.INTERNAL_CRM,
            integration_scope=IntegrationScope.ORGANIZATION,
            name="Copilot CRM",
            credentials={},
            connection_metadata={"source": "internal_crm", "internal": True},
            is_active=True,
        )
        self.org_session.add(integration)
        await self.org_session.flush()
        await self.org_session.refresh(integration)
        return integration

    async def _create_accounts(
        self,
        accounts: list[AccountCandidate],
        integration: KnowledgeIntegration,
    ) -> dict[str, CRMCompany]:
        created: dict[str, CRMCompany] = {}
        now = datetime.now(UTC)
        for candidate in accounts:
            company_id = uuid4()
            company = CRMCompany(
                company_id=company_id,
                organisation_id=self.organisation_id,
                integration_id=integration.integration_id,
                external_id=str(company_id),
                name=candidate.name,
                domain=candidate.domain,
                website=f"https://{candidate.domain}",
                synced_at=now,
                is_deleted=False,
                properties=candidate.properties,
            )
            self.org_session.add(company)
            created[candidate.domain] = company
        if created:
            await self.org_session.flush()
        return created

    async def _create_contacts(
        self,
        contacts: list[ContactCandidate],
        integration: KnowledgeIntegration,
        companies_by_domain: dict[str, CRMCompany],
    ) -> int:
        now = datetime.now(UTC)
        created = 0
        for candidate in contacts:
            company = companies_by_domain.get(candidate.company_domain)
            if company is None:
                continue
            contact_id = uuid4()
            contact = CRMContact(
                contact_id=contact_id,
                external_id=str(contact_id),
                organisation_id=self.organisation_id,
                integration_id=integration.integration_id,
                email=candidate.email,
                first_name=candidate.first_name,
                last_name=candidate.last_name,
                full_name=candidate.full_name,
                company_name=company.name,
                company_id=company.company_id,
                synced_at=now,
                is_deleted=False,
                properties=candidate.properties,
                metrics={},
                additional_emails=[],
            )
            self.org_session.add(contact)
            created += 1
        if created:
            await self.org_session.flush()
        return created

    @staticmethod
    def _normalize_domain(domain: str | None) -> str | None:
        if not domain:
            return None
        cleaned = domain.strip().lower()
        if cleaned.startswith("http://"):
            cleaned = cleaned[7:]
        if cleaned.startswith("https://"):
            cleaned = cleaned[8:]
        if cleaned.startswith("www."):
            cleaned = cleaned[4:]
        cleaned = cleaned.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        return cleaned or None

    @staticmethod
    def _is_resource_attendee(payload: dict | None) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("resource") is True:
            return True
        attendee_type = payload.get("type")
        return isinstance(attendee_type, str) and attendee_type.lower() == "resource"
