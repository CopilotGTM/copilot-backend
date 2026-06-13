"""Resolve calendar invite attendees from explicit emails and product records."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlmodel import col
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import SupabaseUserStatus, User
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
)
from src.service.calendar_invites.models import CalendarInviteAttendee

_DEAL_STAKEHOLDER_PHRASES = {
    "all stakeholders",
    "all stakeholder",
    "stakeholders",
    "all deal stakeholders",
    "deal stakeholders",
    "everyone on the deal",
}


@dataclass(frozen=True)
class AttendeeCandidate:
    """A resolved attendee candidate with provenance for user confirmation."""

    email: str
    source: str
    name: str | None = None
    optional: bool = False
    source_id: str | None = None
    confidence: str = "exact_email"
    input_value: str | None = None

    def to_attendee(self) -> CalendarInviteAttendee:
        """Convert a confirmed candidate into the service-facing attendee DTO."""
        return CalendarInviteAttendee(
            email=self.email,
            name=self.name,
            optional=self.optional,
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Expose stable confirmation metadata without ORM objects."""
        return {
            "email": self.email,
            "name": self.name,
            "optional": self.optional,
            "source": self.source,
            "source_id": self.source_id,
            "confidence": self.confidence,
            "input_value": self.input_value,
        }


@dataclass
class AttendeeResolutionResult:
    """Resolution result consumed by the calendar invite tool."""

    resolved: list[AttendeeCandidate] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    requires_confirmation: bool = False
    confirmation_reasons: list[str] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        """Return whether the invite must not be sent without more user input."""
        return bool(self.ambiguous or self.missing or not self.resolved)

    def service_attendees(self) -> list[CalendarInviteAttendee]:
        """Return validated attendees for the write service."""
        return [candidate.to_attendee() for candidate in self.resolved]

    def public_resolved(self) -> list[dict[str, Any]]:
        """Return structured attendees suitable for HLM confirmation output."""
        return [candidate.to_public_dict() for candidate in self.resolved]

    def add_confirmation_reason(self, reason: str) -> None:
        """Record a confirmation reason once while preserving order."""
        if reason not in self.confirmation_reasons:
            self.confirmation_reasons.append(reason)
        self.requires_confirmation = True


class CalendarInviteAttendeeResolver:
    """Resolve invite attendees inside the organisation boundary.

    The resolver owns fuzzy identity matching. CalendarInviteService intentionally
    remains a write-only boundary that accepts already validated attendee emails.
    """

    def __init__(self, session: Any, *, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def resolve(
        self,
        attendees: list[str] | list[dict[str, Any]],
        *,
        deal_id: str | UUID | None = None,
    ) -> AttendeeResolutionResult:
        """Resolve mixed attendee input into exact emails or user-facing blockers."""
        result = AttendeeResolutionResult()
        default_deal_id = self._parse_uuid(deal_id)
        seen_emails: set[str] = set()

        for raw_attendee in attendees:
            intent = self._coerce_attendee_intent(raw_attendee)
            input_value = intent["value"]
            optional = bool(intent.get("optional", False))

            if not input_value:
                result.missing.append({"input": raw_attendee, "reason": "empty_attendee"})
                continue

            if intent["kind"] == "deal_stakeholders":
                parsed_deal_id = self._parse_uuid(intent.get("deal_id")) or default_deal_id
                if not parsed_deal_id:
                    result.missing.append(
                        {
                            "input": input_value,
                            "reason": "deal_id_required",
                            "message": "Deal context is required for all stakeholders.",
                        }
                    )
                    continue

                candidates, missing = await self._resolve_deal_stakeholders(parsed_deal_id)
                for candidate in candidates:
                    self._append_unique(result.resolved, candidate, seen_emails)
                result.missing.extend(missing)
                result.add_confirmation_reason("deal_stakeholder_group")
                continue

            if intent["kind"] == "email":
                try:
                    candidate = AttendeeCandidate(
                        email=CalendarInviteAttendee(email=input_value).email,
                        name=intent.get("name"),
                        optional=optional,
                        source="direct_email",
                        confidence="exact_email",
                        input_value=input_value,
                    )
                except ValueError:
                    result.missing.append(
                        {
                            "input": input_value,
                            "reason": "invalid_email",
                            "message": "Attendee email address is invalid.",
                        }
                    )
                    continue
                self._append_unique(result.resolved, candidate, seen_emails)
                continue

            matches = await self._search_people(input_value, deal_id=default_deal_id)
            if not matches:
                result.missing.append(
                    {
                        "input": input_value,
                        "reason": "no_match",
                        "message": "No contact, stakeholder, or internal user matched.",
                    }
                )
                continue

            if len(matches) == 1:
                candidate = matches[0]
                self._append_unique(
                    result.resolved,
                    AttendeeCandidate(
                        email=candidate.email,
                        name=candidate.name,
                        optional=optional,
                        source=candidate.source,
                        source_id=candidate.source_id,
                        confidence=candidate.confidence,
                        input_value=input_value,
                    ),
                    seen_emails,
                )
                result.add_confirmation_reason("attendee_resolved_from_name")
                continue

            result.ambiguous.append(
                {
                    "input": input_value,
                    "options": [candidate.to_public_dict() for candidate in matches[:10]],
                }
            )
            result.add_confirmation_reason("attendee_ambiguous")

        return result

    async def _search_people(
        self,
        search: str,
        *,
        deal_id: UUID | None,
    ) -> list[AttendeeCandidate]:
        """Search stakeholders, CRM contacts, and internal users for a name/email."""
        candidates: list[AttendeeCandidate] = []
        candidates.extend(await self._search_stakeholders(search, deal_id=deal_id))
        candidates.extend(await self._search_contacts(search))
        candidates.extend(await self._search_internal_users(search))
        return self._dedupe_candidates(candidates)

    async def _search_stakeholders(
        self,
        search: str,
        *,
        deal_id: UUID | None,
    ) -> list[AttendeeCandidate]:
        lower_search = search.strip().lower()
        name_expr = func.lower(col(CRMDealParticipant.related_name))
        email_expr = func.lower(col(CRMDealParticipant.related_email))
        stmt = (
            select(CRMDealParticipant)
            .where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted) == False,  # noqa: E712
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
                or_(
                    name_expr == lower_search,
                    email_expr == lower_search,
                    col(CRMDealParticipant.related_name).ilike(f"%{search}%"),
                    col(CRMDealParticipant.related_email).ilike(f"%{search}%"),
                ),
            )
            .limit(10)
        )
        if deal_id:
            stmt = stmt.where(col(CRMDealParticipant.deal_id) == deal_id)

        result = await self.session.execute(stmt)
        participants = result.scalars().all()
        return [
            self._candidate_from_participant(participant, search)
            for participant in participants
            if participant.related_email
        ]

    async def _search_contacts(self, search: str) -> list[AttendeeCandidate]:
        lower_search = search.strip().lower()
        full_name_expr = func.concat(
            func.coalesce(col(CRMContact.first_name), ""),
            " ",
            func.coalesce(col(CRMContact.last_name), ""),
        )
        stmt = (
            select(CRMContact)
            .where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted) == False,  # noqa: E712
                col(CRMContact.email).is_not(None),
                or_(
                    func.lower(col(CRMContact.email)) == lower_search,
                    func.lower(col(CRMContact.full_name)) == lower_search,
                    func.lower(full_name_expr) == lower_search,
                    col(CRMContact.email).ilike(f"%{search}%"),
                    col(CRMContact.full_name).ilike(f"%{search}%"),
                    col(CRMContact.first_name).ilike(f"%{search}%"),
                    col(CRMContact.last_name).ilike(f"%{search}%"),
                ),
            )
            .limit(10)
        )
        result = await self.session.execute(stmt)
        contacts = result.scalars().all()
        return [
            self._candidate_from_contact(contact, search) for contact in contacts if contact.email
        ]

    async def _search_internal_users(self, search: str) -> list[AttendeeCandidate]:
        lower_search = search.strip().lower()
        full_name_expr = func.concat(col(User.first_name), " ", col(User.last_name))
        stmt = (
            select(User)
            .where(
                col(User.organisation_id) == self.organisation_id,
                col(User.status) == SupabaseUserStatus.ACTIVE,
                col(User.admin_status) == AdminUserStatus.APPROVED,
                or_(
                    func.lower(col(User.email)) == lower_search,
                    func.lower(full_name_expr) == lower_search,
                    col(User.email).ilike(f"%{search}%"),
                    col(User.first_name).ilike(f"%{search}%"),
                    col(User.last_name).ilike(f"%{search}%"),
                ),
            )
            .limit(10)
        )
        result = await self.session.execute(stmt)
        users = result.scalars().all()
        return [self._candidate_from_user(user, search) for user in users]

    async def _resolve_deal_stakeholders(
        self,
        deal_id: UUID,
    ) -> tuple[list[AttendeeCandidate], list[dict[str, Any]]]:
        stmt = (
            select(CRMDealParticipant)
            .where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.deal_id) == deal_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted) == False,  # noqa: E712
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
            )
            .order_by(
                col(CRMDealParticipant.related_name),
                col(CRMDealParticipant.related_email),
            )
        )
        result = await self.session.execute(stmt)
        participants = result.scalars().all()

        candidates: list[AttendeeCandidate] = []
        missing: list[dict[str, Any]] = []
        for participant in participants:
            if participant.related_email:
                candidates.append(
                    self._candidate_from_participant(
                        participant,
                        "all stakeholders",
                        confidence="deal_group",
                    )
                )
                continue
            missing.append(
                {
                    "input": participant.related_name or str(participant.participant_id),
                    "source": "deal_stakeholder",
                    "source_id": str(participant.participant_id),
                    "reason": "missing_email",
                }
            )

        if not participants:
            missing.append(
                {
                    "input": "all stakeholders",
                    "reason": "no_deal_stakeholders",
                    "message": "No approved stakeholders were found for this deal.",
                }
            )

        return self._dedupe_candidates(candidates), missing

    @staticmethod
    def _coerce_attendee_intent(raw_attendee: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw_attendee, str):
            value = raw_attendee.strip()
            normalized = re.sub(r"\s+", " ", value.lower()).strip(" .!?")
            is_deal_group = normalized in _DEAL_STAKEHOLDER_PHRASES or (
                normalized.startswith("all stakeholders") and "deal" in normalized
            )
            kind = "deal_stakeholders" if is_deal_group else "name"
            if "@" in value:
                kind = "email"
            return {"kind": kind, "value": value}

        value = str(
            raw_attendee.get("value") or raw_attendee.get("email") or raw_attendee.get("name") or ""
        ).strip()
        raw_kind = str(raw_attendee.get("type") or raw_attendee.get("kind") or "").strip().lower()
        kind = raw_kind or ("email" if raw_attendee.get("email") else "name")
        if kind in {"stakeholders", "deal_stakeholder", "deal_stakeholders"}:
            kind = "deal_stakeholders"
            value = value or "all stakeholders"
        elif kind not in {"email", "name", "person_name"}:
            kind = "email" if "@" in value else "name"
        elif kind == "person_name":
            kind = "name"

        return {
            "kind": kind,
            "value": value,
            "name": raw_attendee.get("name"),
            "optional": raw_attendee.get("optional", False),
            "deal_id": raw_attendee.get("deal_id"),
        }

    @staticmethod
    def _parse_uuid(value: str | UUID | None) -> UUID | None:
        if isinstance(value, UUID):
            return value
        if not value:
            return None
        try:
            return UUID(str(value))
        except ValueError:
            return None

    @staticmethod
    def _append_unique(
        target: list[AttendeeCandidate],
        candidate: AttendeeCandidate,
        seen_emails: set[str],
    ) -> None:
        email = candidate.email.lower()
        if email in seen_emails:
            return
        seen_emails.add(email)
        target.append(candidate)

    @staticmethod
    def _dedupe_candidates(candidates: list[AttendeeCandidate]) -> list[AttendeeCandidate]:
        deduped: list[AttendeeCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            email = candidate.email.lower()
            if email in seen:
                continue
            seen.add(email)
            deduped.append(candidate)
        return deduped

    @staticmethod
    def _confidence_for(search: str, *, name: str | None, email: str | None) -> str:
        normalized = search.strip().lower()
        if email and email.lower() == normalized:
            return "exact_email"
        if name and re.sub(r"\s+", " ", name.strip().lower()) == normalized:
            return "exact_name"
        return "partial_name"

    def _candidate_from_participant(
        self,
        participant: CRMDealParticipant,
        search: str,
        *,
        confidence: str | None = None,
    ) -> AttendeeCandidate:
        return AttendeeCandidate(
            email=CalendarInviteAttendee(email=participant.related_email or "").email,
            name=participant.related_name,
            source="deal_stakeholder",
            source_id=str(participant.participant_id),
            confidence=confidence
            or self._confidence_for(
                search,
                name=participant.related_name,
                email=participant.related_email,
            ),
            input_value=search,
        )

    def _candidate_from_contact(self, contact: CRMContact, search: str) -> AttendeeCandidate:
        name = contact.full_name or " ".join(
            part for part in [contact.first_name, contact.last_name] if part
        )
        return AttendeeCandidate(
            email=CalendarInviteAttendee(email=contact.email or "").email,
            name=name or None,
            source="contact",
            source_id=str(contact.contact_id),
            confidence=self._confidence_for(search, name=name, email=contact.email),
            input_value=search,
        )

    def _candidate_from_user(self, user: User, search: str) -> AttendeeCandidate:
        name = " ".join(part for part in [user.first_name, user.last_name] if part)
        return AttendeeCandidate(
            email=CalendarInviteAttendee(email=str(user.email)).email,
            name=name or None,
            source="internal_user",
            source_id=str(user.user_id),
            confidence=self._confidence_for(search, name=name, email=str(user.email)),
            input_value=search,
        )
