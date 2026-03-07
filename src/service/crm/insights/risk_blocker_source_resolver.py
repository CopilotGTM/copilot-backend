"""Helpers for mapping raw provider metadata to risk/blocker source enums."""

from __future__ import annotations

from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.domain.models.organisation_tables.sync.meeting_intelligence import MeetingSource
from src.domain.models.organisation_tables.sync.risk_blocker_models import RiskBlockerSource


def _parse_integration_type(value: IntegrationType | str | None) -> IntegrationType | None:
    if value is None:
        return None
    if isinstance(value, IntegrationType):
        return value
    try:
        return IntegrationType(str(value))
    except ValueError:
        return None


def _parse_meeting_source(value: MeetingSource | str | None) -> MeetingSource | None:
    if value is None:
        return None
    if isinstance(value, MeetingSource):
        return value
    try:
        return MeetingSource(str(value))
    except ValueError:
        return None


def resolve_email_risk_blocker_source(
    integration_type: IntegrationType | str | None,
) -> RiskBlockerSource | None:
    """Map an email integration type to the persisted risk/blocker source enum."""
    parsed = _parse_integration_type(integration_type)
    if parsed == IntegrationType.GMAIL:
        return RiskBlockerSource.EMAIL_GMAIL
    if parsed == IntegrationType.OUTLOOK_EMAIL:
        return RiskBlockerSource.EMAIL_OUTLOOK
    return None


def resolve_meeting_risk_blocker_source(
    *,
    meeting_source: MeetingSource | str | None = None,
    online_meeting_provider: str | None = None,
    conference_link: str | None = None,
    calendar_integration_type: IntegrationType | str | None = None,
) -> RiskBlockerSource | None:
    """Map meeting metadata to the persisted risk/blocker source enum.

    Precedence:
    1. Explicit transcript source if present
    2. Online meeting provider / conference link
    3. Calendar integration type as fallback
    """
    parsed_meeting_source = _parse_meeting_source(meeting_source)
    if parsed_meeting_source == MeetingSource.TEAMS_TRANSCRIPT:
        return RiskBlockerSource.TEAMS_MEETING
    if parsed_meeting_source == MeetingSource.ZOOM_RECORDING:
        return RiskBlockerSource.ZOOM_MEETING
    if parsed_meeting_source == MeetingSource.GOOGLE_MEET:
        return RiskBlockerSource.MEETING_GOOGLE

    provider = (online_meeting_provider or "").strip().lower()
    link = (conference_link or "").strip().lower()
    if "teams" in provider or "teams.microsoft.com" in link or "teams.live.com" in link:
        return RiskBlockerSource.TEAMS_MEETING
    if "zoom" in provider or "zoom.us" in link:
        return RiskBlockerSource.ZOOM_MEETING
    if "google" in provider or provider == "meet" or "meet.google.com" in link:
        return RiskBlockerSource.MEETING_GOOGLE

    parsed_integration = _parse_integration_type(calendar_integration_type)
    if parsed_integration == IntegrationType.GOOGLE_CALENDAR:
        return RiskBlockerSource.MEETING_GOOGLE
    if parsed_integration == IntegrationType.OUTLOOK_CALENDAR:
        return RiskBlockerSource.MEETING_OUTLOOK
    return None
