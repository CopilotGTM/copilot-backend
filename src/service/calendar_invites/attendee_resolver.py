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
from src.domain.models.organisation_tables.sync.crm.activities import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
    CRMActivityType,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealMutationEvent,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadParticipantType,
    CRMLeadStatus,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
)
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.service.calendar_invites.models import (
    MAX_CALENDAR_INVITE_ATTENDEES,
    CalendarInviteAttendee,
)
from src.service.crm.access.crm_user_directory_service import (
    CRMUserDirectoryResult,
    CRMUserDirectoryService,
)
from src.service.crm.contacts.read_service import ContactReadService

_DEAL_STAKEHOLDER_PHRASES = {
    "all stakeholders",
    "all stakeholder",
    "stakeholders",
    "all deal stakeholders",
    "deal stakeholders",
    "everyone on the deal",
}
# Fuzzy matching walks several user-context tiers. Exact matching is batched,
# while this small remainder keeps worst-case database work bounded.
MAX_FUZZY_ATTENDEE_NAMES = 10


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

    def __init__(
        self,
        session: Any,
        *,
        organisation_id: UUID,
        requested_by_user_id: UUID | None = None,
        user_email: str | None = None,
    ) -> None:
        """Bind identity lookup to the tenant and requesting user's commercial graph."""
        self.session = session
        self.organisation_id = organisation_id
        self.requested_by_user_id = requested_by_user_id
        self.user_email = (user_email or "").strip().lower() or None

    async def resolve(
        self,
        attendees: list[str] | list[dict[str, Any]],
        *,
        deal_id: str | UUID | None = None,
    ) -> AttendeeResolutionResult:
        """Resolve mixed attendee input with bounded user-centric database work."""
        result = AttendeeResolutionResult()
        if len(attendees) > MAX_CALENDAR_INVITE_ATTENDEES:
            result.missing.append(
                {
                    "input": f"{len(attendees)} attendees",
                    "reason": "attendee_limit_exceeded",
                    "message": (
                        f"Calendar invites support at most "
                        f"{MAX_CALENDAR_INVITE_ATTENDEES} attendees."
                    ),
                }
            )
            return result

        default_deal_id = self._parse_uuid(deal_id)
        seen_emails: set[str] = set()
        name_intents: dict[str, dict[str, Any]] = {}

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

            # Repeated names should not repeat the same multi-tier lookup. The
            # first occurrence keeps its required/optional role, matching email dedupe.
            name_intents.setdefault(self._normalize_identity(input_value), intent)

        if not name_intents:
            return result

        # Explicit "from our team" language is a hard entity boundary. Resolve those
        # names from crm_users only, so a commercially relevant contact cannot outrank
        # the colleague the user asked for.
        team_name_intents = {
            normalized_name: intent
            for normalized_name, intent in name_intents.items()
            if intent.get("scope") == "team"
        }
        if len(team_name_intents) > MAX_FUZZY_ATTENDEE_NAMES:
            # Team lookup is exact-first but still allows fuzzy fallback. Keep the same
            # ten-name ceiling as unscoped fuzzy resolution to bound database work.
            for normalized_name, intent in team_name_intents.items():
                result.missing.append(
                    {
                        "input": intent["value"],
                        "reason": "name_lookup_limit_exceeded",
                        "message": (
                            "Too many team attendees need name matching; provide exact "
                            "email addresses for those attendees."
                        ),
                    }
                )
                name_intents.pop(normalized_name)
            return result
        if team_name_intents:
            directory = CRMUserDirectoryService(self.session, self.organisation_id)
            for normalized_name, intent in team_name_intents.items():
                team_resolution = await directory.search(intent["value"])
                self._record_team_name_resolution(
                    result,
                    resolution=team_resolution,
                    input_value=intent["value"],
                    optional=bool(intent.get("optional", False)),
                    seen_emails=seen_emails,
                )
                name_intents.pop(normalized_name)

        if not name_intents:
            return result

        # Exact names are searched once per source tier for the complete input set.
        # This retains the user-centric priority without multiplying queries by names.
        exact_matches = await self._search_exact_people(
            set(name_intents),
            deal_id=default_deal_id,
        )
        unresolved_names = [name for name in name_intents if name not in exact_matches]
        if len(unresolved_names) > MAX_FUZZY_ATTENDEE_NAMES:
            for normalized_name in unresolved_names:
                result.missing.append(
                    {
                        "input": name_intents[normalized_name]["value"],
                        "reason": "name_lookup_limit_exceeded",
                        "message": (
                            "Too many attendees need fuzzy name matching; provide exact "
                            "email addresses for the unresolved attendees."
                        ),
                    }
                )
            return result

        for normalized_name, intent in name_intents.items():
            matches = exact_matches.get(normalized_name)
            if matches is None:
                matches = await self._search_people(
                    intent["value"],
                    deal_id=default_deal_id,
                )
            self._record_name_resolution(
                result,
                matches=matches,
                input_value=intent["value"],
                optional=bool(intent.get("optional", False)),
                seen_emails=seen_emails,
            )

        return result

    def _record_name_resolution(
        self,
        result: AttendeeResolutionResult,
        *,
        matches: list[AttendeeCandidate],
        input_value: str,
        optional: bool,
        seen_emails: set[str],
    ) -> None:
        """Record one name lookup while preserving confirmation and ambiguity rules."""
        if not matches:
            result.missing.append(
                {
                    "input": input_value,
                    "reason": "no_match",
                    "message": "No contact, stakeholder, or internal user matched.",
                }
            )
            return

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
            return

        result.ambiguous.append(
            {
                "input": input_value,
                "options": [candidate.to_public_dict() for candidate in matches[:10]],
            }
        )
        result.add_confirmation_reason("attendee_ambiguous")

    def _record_team_name_resolution(
        self,
        result: AttendeeResolutionResult,
        *,
        resolution: CRMUserDirectoryResult,
        input_value: str,
        optional: bool,
        seen_emails: set[str],
    ) -> None:
        """Record a team-only CRM-user match without falling back to contacts."""
        if resolution.status == "not_found":
            result.missing.append(
                {
                    "input": input_value,
                    "reason": "no_team_match",
                    "message": "No active CRM user from your team matched.",
                }
            )
            return

        if resolution.status == "ambiguous":
            result.ambiguous.append(
                {
                    "input": input_value,
                    "options": [
                        {
                            "email": user.email,
                            "name": user.full_name
                            or " ".join(part for part in (user.first_name, user.last_name) if part)
                            or None,
                            "optional": optional,
                            "source": "crm_user",
                            "source_id": str(user.user_id),
                            "confidence": "name_match",
                            "input_value": input_value,
                        }
                        for user in resolution.candidates
                    ],
                }
            )
            result.add_confirmation_reason("attendee_ambiguous")
            return

        user = resolution.candidates[0]
        if not user.email:
            result.missing.append(
                {
                    "input": input_value,
                    "source": "crm_user",
                    "source_id": str(user.user_id),
                    "reason": "missing_email",
                    "message": "The matching team member has no CRM email address.",
                }
            )
            return

        name = user.full_name or " ".join(
            part for part in (user.first_name, user.last_name) if part
        )
        self._record_name_resolution(
            result,
            matches=[
                AttendeeCandidate(
                    email=user.email,
                    name=name or None,
                    source="crm_user",
                    source_id=str(user.user_id),
                    confidence="exact_name",
                    input_value=input_value,
                )
            ],
            input_value=input_value,
            optional=optional,
            seen_emails=seen_emails,
        )

    async def _search_exact_people(
        self,
        searches: set[str],
        *,
        deal_id: UUID | None,
    ) -> dict[str, list[AttendeeCandidate]]:
        """Resolve exact identities in fixed-count tiers before fuzzy per-name lookup."""
        unresolved = set(searches)
        resolved: dict[str, list[AttendeeCandidate]] = {}

        def claim(candidates: list[AttendeeCandidate]) -> None:
            """Claim only the highest-priority tier that matched each input name."""
            grouped: dict[str, list[AttendeeCandidate]] = {}
            for candidate in candidates:
                if candidate.input_value in unresolved:
                    grouped.setdefault(candidate.input_value, []).append(candidate)
            for search, matches in grouped.items():
                resolved[search] = self._prefer_unique_exact(self._dedupe_candidates(matches))
                unresolved.remove(search)

        if deal_id:
            claim(await self._search_stakeholders(unresolved, deal_id=deal_id))
        if self.requested_by_user_id:
            for search_stage in (
                self._search_user_deal_contacts,
                self._search_user_lead_contacts,
                self._search_user_added_contacts,
                self._search_recent_activity_contacts,
            ):
                if not unresolved:
                    break
                claim(await search_stage(unresolved))

        if unresolved:
            # Organization contacts and internal users share the broad fallback
            # tier, so namesakes across those sources remain ambiguous.
            candidates = await self._search_contacts(unresolved)
            candidates.extend(await self._search_internal_users(unresolved))
            claim(candidates)
        return resolved

    async def _search_people(
        self,
        search: str,
        *,
        deal_id: UUID | None,
    ) -> list[AttendeeCandidate]:
        """Search the requester's working set before the organization-wide fallback.

        Each stage stops only when it finds plausible matches. This preserves
        ambiguity within a commercial context while preventing thousands of
        unrelated organization contacts from competing with an active deal.
        """
        if deal_id:
            candidates = self._prefer_unique_exact(
                self._dedupe_candidates(await self._search_stakeholders(search, deal_id=deal_id))
            )
            if candidates:
                return candidates
        if self.requested_by_user_id:
            for search_stage in (
                self._search_user_deal_contacts,
                self._search_user_lead_contacts,
                self._search_user_added_contacts,
                self._search_recent_activity_contacts,
            ):
                candidates = self._prefer_unique_exact(
                    self._dedupe_candidates(await search_stage(search))
                )
                if candidates:
                    return candidates

        # Reuse the indexed fuzzy search used by the CRM agent instead of keeping
        # a second organization-wide matching algorithm in the calendar feature.
        candidates = await self._search_contacts(search)
        candidates.extend(await self._search_internal_users(search))
        return self._prefer_unique_exact(self._dedupe_candidates(candidates))

    async def _search_user_deal_contacts(
        self,
        search: str | set[str],
    ) -> list[AttendeeCandidate]:
        """Return name matches from the requester's open deals."""
        if not self.requested_by_user_id:
            return []
        participant = CRMDealParticipant
        deal = CRMDeal
        stmt = (
            select(participant)
            .join(deal, col(participant.deal_id) == col(deal.deal_id))
            .where(
                col(participant.organisation_id) == self.organisation_id,
                col(deal.organisation_id) == self.organisation_id,
                col(participant.participant_type) == CRMParticipantType.CONTACT.value,
                col(participant.is_deleted) == False,  # noqa: E712
                col(deal.is_deleted) == False,  # noqa: E712
                col(participant.approval_status) == ApprovalStatus.APPROVED.value,
                col(deal.deal_status) == CRMDealStatus.OPEN.value,
                self._requester_owns_record(deal),
                self._participant_matches(
                    search,
                    name_column=col(participant.related_name),
                    email_column=col(participant.related_email),
                ),
            )
            .order_by(col(deal.last_activity_at).desc().nullslast())
            .limit(self._search_limit(search))
        )
        result = await self.session.execute(stmt)
        return [
            self._candidate_from_participant(
                row,
                self._candidate_search(
                    search,
                    name=row.related_name,
                    email=row.related_email,
                ),
                source="user_open_deal",
            )
            for row in result.scalars().all()
            if row.related_email
        ]

    async def _search_user_lead_contacts(
        self,
        search: str | set[str],
    ) -> list[AttendeeCandidate]:
        """Return contacts and direct identities on the requester's active prospects."""
        if not self.requested_by_user_id:
            return []
        active_statuses = {
            CRMLeadStatus.NEW.value,
            CRMLeadStatus.WORKING.value,
            CRMLeadStatus.QUALIFIED.value,
        }
        lead_stmt = (
            select(CRMLead)
            .where(
                col(CRMLead.organisation_id) == self.organisation_id,
                col(CRMLead.is_deleted) == False,  # noqa: E712
                col(CRMLead.status).in_(active_statuses),
                self._requester_owns_record(CRMLead),
                col(CRMLead.email).is_not(None),
                self._participant_matches(
                    search,
                    name_column=col(CRMLead.full_name),
                    email_column=col(CRMLead.email),
                ),
            )
            .order_by(col(CRMLead.last_activity_at).desc().nullslast())
            .limit(self._search_limit(search))
        )
        lead_result = await self.session.execute(lead_stmt)
        candidates = [
            AttendeeCandidate(
                email=CalendarInviteAttendee(email=lead.email or "").email,
                name=lead.full_name,
                source="user_active_prospect",
                source_id=str(lead.lead_id),
                confidence=self._confidence_for(
                    self._candidate_search(search, name=lead.full_name, email=lead.email),
                    name=lead.full_name,
                    email=lead.email,
                ),
                input_value=self._candidate_search(
                    search,
                    name=lead.full_name,
                    email=lead.email,
                ),
            )
            for lead in lead_result.scalars().all()
            if lead.email
        ]

        participant_stmt = (
            select(CRMLeadParticipant)
            .join(CRMLead, col(CRMLeadParticipant.lead_id) == col(CRMLead.lead_id))
            .where(
                col(CRMLeadParticipant.organisation_id) == self.organisation_id,
                col(CRMLead.organisation_id) == self.organisation_id,
                col(CRMLeadParticipant.is_deleted) == False,  # noqa: E712
                col(CRMLead.is_deleted) == False,  # noqa: E712
                col(CRMLead.status).in_(active_statuses),
                col(CRMLeadParticipant.participant_type) == CRMLeadParticipantType.CONTACT.value,
                self._requester_owns_record(CRMLead),
                col(CRMLeadParticipant.email).is_not(None),
                self._participant_matches(
                    search,
                    name_column=col(CRMLeadParticipant.name),
                    email_column=col(CRMLeadParticipant.email),
                ),
            )
            .order_by(col(CRMLead.last_activity_at).desc().nullslast())
            .limit(self._search_limit(search))
        )
        participant_result = await self.session.execute(participant_stmt)
        candidates.extend(
            AttendeeCandidate(
                email=CalendarInviteAttendee(email=row.email or "").email,
                name=row.name,
                source="user_active_prospect",
                source_id=str(row.participant_id),
                confidence=self._confidence_for(
                    self._candidate_search(search, name=row.name, email=row.email),
                    name=row.name,
                    email=row.email,
                ),
                input_value=self._candidate_search(
                    search,
                    name=row.name,
                    email=row.email,
                ),
            )
            for row in participant_result.scalars().all()
            if row.email
        )
        return candidates

    async def _search_user_added_contacts(
        self,
        search: str | set[str],
    ) -> list[AttendeeCandidate]:
        """Return contacts explicitly owned or created by the requesting user."""
        if not self.requested_by_user_id:
            return []
        actor_contact_ids = select(col(CRMDealMutationEvent.entity_id)).where(
            col(CRMDealMutationEvent.organisation_id) == self.organisation_id,
            col(CRMDealMutationEvent.entity_type) == "contact",
            col(CRMDealMutationEvent.operation) == "create_contact",
            col(CRMDealMutationEvent.actor_user_id) == self.requested_by_user_id,
        )
        ownership = [
            col(CRMContact.owner_id) == self.requested_by_user_id,
            col(CRMContact.contact_id).in_(actor_contact_ids),
        ]
        if self.user_email:
            ownership.append(func.lower(func.trim(col(CRMContact.owner_email))) == self.user_email)
        stmt = (
            select(CRMContact)
            .where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted) == False,  # noqa: E712
                col(CRMContact.email).is_not(None),
                or_(*ownership),
                self._contact_matches(search),
            )
            .order_by(col(CRMContact.last_activity_at).desc().nullslast())
            .limit(self._search_limit(search))
        )
        result = await self.session.execute(stmt)
        return [
            self._candidate_from_contact(
                contact,
                self._candidate_search(search, name=contact.full_name, email=contact.email),
                source="user_added_contact",
            )
            for contact in result.scalars().all()
            if contact.email
        ]

    async def _search_recent_activity_contacts(
        self,
        search: str | set[str],
    ) -> list[AttendeeCandidate]:
        """Return contacts with recent projected activity from the requester's mailbox."""
        if not self.requested_by_user_id:
            return []

        # Email hydration has already materialized both the contact edge and its
        # source integration. Reusing that projection avoids loading message
        # subjects or bodies merely to rediscover an existing CRM contact.
        recent_activity = (
            select(
                col(CRMActivityLink.object_id).label("contact_id"),
                func.max(col(CRMActivityLink.occurred_at)).label("last_user_activity_at"),
            )
            .join(
                CRMActivity,
                col(CRMActivityLink.activity_id) == col(CRMActivity.activity_id),
            )
            .join(
                KnowledgeIntegration,
                col(CRMActivity.source_integration_id) == col(KnowledgeIntegration.integration_id),
            )
            .where(
                col(CRMActivityLink.organisation_id) == self.organisation_id,
                col(CRMActivity.organisation_id) == self.organisation_id,
                col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                col(KnowledgeIntegration.created_by_user_id) == self.requested_by_user_id,
                col(CRMActivityLink.object_type) == CRMActivityObjectType.CONTACT.value,
                col(CRMActivity.activity_type) == CRMActivityType.EMAIL.value,
                col(CRMActivity.is_deleted) == False,  # noqa: E712
            )
            .group_by(col(CRMActivityLink.object_id))
            .subquery()
        )
        stmt = (
            select(CRMContact)
            .join(
                recent_activity,
                col(CRMContact.contact_id) == recent_activity.c.contact_id,
            )
            .where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted) == False,  # noqa: E712
                col(CRMContact.email).is_not(None),
                self._contact_matches(search),
            )
            .order_by(recent_activity.c.last_user_activity_at.desc())
            .limit(self._search_limit(search))
        )
        result = await self.session.execute(stmt)
        return [
            self._candidate_from_contact(
                contact,
                self._candidate_search(search, name=contact.full_name, email=contact.email),
                source="recent_user_email",
            )
            for contact in result.scalars().all()
            if contact.email
        ]

    async def _search_stakeholders(
        self,
        search: str | set[str],
        *,
        deal_id: UUID | None,
    ) -> list[AttendeeCandidate]:
        stmt = (
            select(CRMDealParticipant)
            .where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted) == False,  # noqa: E712
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
                self._participant_matches(
                    search,
                    name_column=col(CRMDealParticipant.related_name),
                    email_column=col(CRMDealParticipant.related_email),
                ),
            )
            .limit(self._search_limit(search))
        )
        if deal_id:
            stmt = stmt.where(col(CRMDealParticipant.deal_id) == deal_id)

        result = await self.session.execute(stmt)
        participants = result.scalars().all()
        return [
            self._candidate_from_participant(
                participant,
                self._candidate_search(
                    search,
                    name=participant.related_name,
                    email=participant.related_email,
                ),
            )
            for participant in participants
            if participant.related_email
        ]

    async def _search_contacts(self, search: str | set[str]) -> list[AttendeeCandidate]:
        """Use the CRM agent's indexed fuzzy search as the broad fallback."""
        if isinstance(search, set):
            stmt = (
                select(CRMContact)
                .where(
                    col(CRMContact.organisation_id) == self.organisation_id,
                    col(CRMContact.is_deleted) == False,  # noqa: E712
                    col(CRMContact.email).is_not(None),
                    self._contact_matches(search),
                )
                .limit(self._search_limit(search))
            )
            result = await self.session.execute(stmt)
            rows = [{"contact": contact} for contact in result.scalars().all()]
        else:
            rows = await ContactReadService(
                self.session,
                self.organisation_id,
            ).search_contacts_for_agent(q=search, limit=20)
        return [
            self._candidate_from_contact(
                row["contact"],
                self._candidate_search(
                    search,
                    name=row["contact"].full_name,
                    email=row["contact"].email,
                ),
                source="organization_contact",
            )
            for row in rows
            if row["contact"].email
        ]

    def _requester_owns_record(self, model: Any) -> Any:
        """Build the user-ownership predicate shared by deal and lead queries."""
        conditions = [
            col(model.owner_id) == self.requested_by_user_id,
            col(model.created_by_user_id) == self.requested_by_user_id,
        ]
        if self.user_email:
            conditions.append(func.lower(func.trim(col(model.owner_email))) == self.user_email)
        return or_(*conditions)

    @classmethod
    def _participant_matches(
        cls,
        search: str | set[str],
        *,
        name_column: Any,
        email_column: Any,
    ) -> Any:
        """Build an exact-or-substring identity predicate for relationship rows."""
        if isinstance(search, set):
            return or_(
                func.lower(func.trim(email_column)).in_(search),
                func.lower(func.trim(name_column)).in_(search),
            )
        normalized = cls._normalize_identity(search)
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return or_(
            func.lower(func.trim(email_column)) == normalized,
            func.lower(func.trim(name_column)) == normalized,
            name_column.ilike(f"%{escaped}%", escape="\\"),
            email_column.ilike(f"%{escaped}%", escape="\\"),
        )

    @classmethod
    def _contact_matches(cls, search: str | set[str]) -> Any:
        """Match a contact by canonical name components or primary email."""
        full_name_expr = func.concat(
            func.coalesce(col(CRMContact.first_name), ""),
            " ",
            func.coalesce(col(CRMContact.last_name), ""),
        )
        if isinstance(search, set):
            return or_(
                func.lower(func.trim(col(CRMContact.email))).in_(search),
                func.lower(func.trim(col(CRMContact.full_name))).in_(search),
                func.lower(func.trim(full_name_expr)).in_(search),
            )
        normalized = cls._normalize_identity(search)
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return or_(
            func.lower(func.trim(col(CRMContact.email))) == normalized,
            func.lower(func.trim(col(CRMContact.full_name))) == normalized,
            func.lower(func.trim(full_name_expr)) == normalized,
            col(CRMContact.email).ilike(f"%{escaped}%", escape="\\"),
            col(CRMContact.full_name).ilike(f"%{escaped}%", escape="\\"),
            col(CRMContact.first_name).ilike(f"%{escaped}%", escape="\\"),
            col(CRMContact.last_name).ilike(f"%{escaped}%", escape="\\"),
        )

    async def _search_internal_users(
        self,
        search: str | set[str],
    ) -> list[AttendeeCandidate]:
        full_name_expr = func.concat(col(User.first_name), " ", col(User.last_name))
        if isinstance(search, set):
            identity_filter = or_(
                func.lower(func.trim(col(User.email))).in_(search),
                func.lower(func.trim(full_name_expr)).in_(search),
            )
        else:
            lower_search = self._normalize_identity(search)
            identity_filter = or_(
                func.lower(col(User.email)) == lower_search,
                func.lower(full_name_expr) == lower_search,
                col(User.email).ilike(f"%{search}%"),
                col(User.first_name).ilike(f"%{search}%"),
                col(User.last_name).ilike(f"%{search}%"),
            )
        stmt = (
            select(User)
            .where(
                col(User.organisation_id) == self.organisation_id,
                col(User.status) == SupabaseUserStatus.ACTIVE,
                col(User.admin_status) == AdminUserStatus.APPROVED,
                identity_filter,
            )
            .limit(self._search_limit(search))
        )
        result = await self.session.execute(stmt)
        users = result.scalars().all()
        return [
            self._candidate_from_user(
                user,
                self._candidate_search(
                    search,
                    name=" ".join(part for part in [user.first_name, user.last_name] if part),
                    email=str(user.email),
                ),
            )
            for user in users
        ]

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
        contacts_by_id = await self._contact_profiles_for_participants(participants)

        candidates: list[AttendeeCandidate] = []
        missing: list[dict[str, Any]] = []
        for participant in participants:
            contact = contacts_by_id.get(participant.related_local_id)
            email = contact.email if contact and contact.email else participant.related_email
            name = contact.full_name if contact and contact.full_name else participant.related_name
            if email:
                candidates.append(
                    self._candidate_from_participant(
                        participant,
                        "all stakeholders",
                        contact=contact,
                        confidence="deal_group",
                    )
                )
                continue
            missing.append(
                {
                    "input": name or str(participant.participant_id),
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

    async def _contact_profiles_for_participants(
        self,
        participants: list[CRMDealParticipant],
    ) -> dict[UUID, CRMContact]:
        contact_ids = {
            participant.related_local_id
            for participant in participants
            if participant.related_local_id
        }
        if not contact_ids:
            return {}
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).in_(contact_ids),
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        result = await self.session.execute(stmt)
        return {contact.contact_id: contact for contact in result.scalars().all()}

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
            "scope": (
                "team" if str(raw_attendee.get("scope") or "").strip().lower() == "team" else None
            ),
        }

    @staticmethod
    def _normalize_identity(value: str) -> str:
        """Normalize user-entered identity text for exact batch matching and dedupe."""
        return re.sub(r"\s+", " ", value.strip().lower())

    @classmethod
    def _candidate_search(
        cls,
        search: str | set[str],
        *,
        name: str | None,
        email: str | None,
    ) -> str:
        """Return the batched input that exactly matched a candidate identity."""
        if isinstance(search, str):
            return search
        identities = {cls._normalize_identity(value) for value in (name, email) if value}
        return next((value for value in search if value in identities), "")

    @staticmethod
    def _search_limit(search: str | set[str]) -> int:
        """Allow up to ten namesakes per batched input without an unbounded result set."""
        return 10 if isinstance(search, str) else max(10, len(search) * 10)

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
    def _prefer_unique_exact(candidates: list[AttendeeCandidate]) -> list[AttendeeCandidate]:
        """Collapse a tier only when one candidate has the sole exact identity match."""
        exact = [
            candidate
            for candidate in candidates
            if candidate.confidence in {"exact_email", "exact_name"}
        ]
        return exact if len(exact) == 1 else candidates

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
        contact: CRMContact | None = None,
        confidence: str | None = None,
        source: str = "deal_stakeholder",
    ) -> AttendeeCandidate:
        email = contact.email if contact and contact.email else participant.related_email
        name = contact.full_name if contact and contact.full_name else participant.related_name
        return AttendeeCandidate(
            email=CalendarInviteAttendee(email=email or "").email,
            name=name,
            source=source,
            source_id=str(participant.participant_id),
            confidence=confidence
            or self._confidence_for(
                search,
                name=name,
                email=email,
            ),
            input_value=search,
        )

    def _candidate_from_contact(
        self,
        contact: CRMContact,
        search: str,
        *,
        source: str = "contact",
    ) -> AttendeeCandidate:
        name = contact.full_name or " ".join(
            part for part in [contact.first_name, contact.last_name] if part
        )
        return AttendeeCandidate(
            email=CalendarInviteAttendee(email=contact.email or "").email,
            name=name or None,
            source=source,
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
