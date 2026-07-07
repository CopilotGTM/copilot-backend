"""Organisation policy checks for meeting recording bots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.infra.database.sessions import acquire_master_async_session
from src.service.crm.utils.identity import extract_email_domain
from src.service.crm.utils.internal_domains import normalize_internal_domains
from src.service.orgnisation.organisation_preferences import (
    BOT_JOIN_POLICY_ALL,
    BOT_JOIN_POLICY_EXTERNAL_ONLY,
    BOT_JOIN_POLICY_NEVER,
    normalize_organisation_preferences,
)

MEETING_RECORDING_POLICY_EXTERNAL_ONLY_REASON = "meeting_recording_policy_external_only"
MEETING_RECORDING_POLICY_NEVER_REASON = "meeting_recording_policy_never"


@dataclass(frozen=True)
class MeetingRecordingPolicyContext:
    bot_join_policy: str
    internal_domains: set[str]


@dataclass(frozen=True)
class MeetingRecordingPolicyResult:
    allowed: bool
    reason: str | None = None
    is_external: bool = False


def is_external_meeting(event: Any, internal_domains: set[str]) -> bool:
    """Return whether at least one attendee is outside the organisation domains."""
    if not internal_domains:
        return False
    for attendee in getattr(event, "attendees", None) or []:
        domain = extract_email_domain(getattr(attendee, "email", None))
        if domain and domain not in internal_domains:
            return True
    return False


def evaluate_bot_join_policy(
    *,
    bot_join_policy: str,
    event: Any,
    internal_domains: set[str],
) -> MeetingRecordingPolicyResult:
    """Evaluate the org bot policy for one already-basic-eligible event."""
    if bot_join_policy == BOT_JOIN_POLICY_ALL:
        return MeetingRecordingPolicyResult(
            True, is_external=is_external_meeting(event, internal_domains)
        )
    if bot_join_policy == BOT_JOIN_POLICY_NEVER:
        return MeetingRecordingPolicyResult(False, MEETING_RECORDING_POLICY_NEVER_REASON)

    external = is_external_meeting(event, internal_domains)
    if bot_join_policy == BOT_JOIN_POLICY_EXTERNAL_ONLY and external:
        return MeetingRecordingPolicyResult(True, is_external=True)
    return MeetingRecordingPolicyResult(
        False,
        MEETING_RECORDING_POLICY_EXTERNAL_ONLY_REASON,
        is_external=False,
    )


async def load_meeting_recording_policy_context(
    organisation_id: UUID,
) -> MeetingRecordingPolicyContext:
    """Load the normalised org recording policy and internal domains."""
    stmt = select(Organisation).where(col(Organisation.organisation_id) == organisation_id)
    organisation = None
    async for master_session in acquire_master_async_session():
        organisation = (await master_session.execute(stmt)).scalar_one_or_none()
        break

    preferences = normalize_organisation_preferences(
        organisation.preferences if organisation is not None else None
    )
    return MeetingRecordingPolicyContext(
        bot_join_policy=preferences["meeting_recording"]["bot_join_policy"],
        internal_domains=normalize_internal_domains(
            getattr(organisation, "domain", None) if organisation is not None else None
        ),
    )
