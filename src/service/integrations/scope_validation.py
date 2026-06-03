"""Scope validation helpers for provider integrations.

This module owns two contracts: canonical required scopes per provider feature and
permissive matching for broader-but-equivalent grants. Callers share this so
permission checks and UI diagnostics stay consistent across setup, validation,
and routes.
"""

from __future__ import annotations

import re

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.google_integration_scopes import (
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_CALENDAR_READONLY,
    GOOGLE_SCOPE_GMAIL_READONLY,
    GOOGLE_SCOPE_GMAIL_SEND,
    GOOGLE_SCOPE_MEETINGS_SPACE_READONLY,
    get_google_required_granted_scopes,
)
from src.infra.config.microsoft_integration_scopes import (
    MICROSOFT_INTEGRATION_SCOPES,
    MICROSOFT_SCOPE_CALENDARS_READ_WRITE,
    MICROSOFT_SCOPE_MAIL_SEND,
    MICROSOFT_SCOPE_OFFLINE_ACCESS,
    MICROSOFT_SCOPE_USER_READ,
    MICROSOFT_SCOPE_USER_READ_ALL,
)
from src.infra.config.slack_integration_scopes import SLACK_USER_SCOPES

_MICROSOFT_INTEGRATION_TYPES = frozenset(
    {
        IntegrationType.OUTLOOK_EMAIL,
        IntegrationType.OUTLOOK_CALENDAR,
        IntegrationType.TEAMS,
    }
)

_GOOGLE_INTEGRATION_TYPES = frozenset(
    {
        IntegrationType.GMAIL,
        IntegrationType.GOOGLE_CALENDAR,
    }
)

# Base required permissions by integration type.
# Keep this map as the single source of truth for what a feature expects to
# have before attempting provider calls.
_REQUIRED_SCOPES: dict[IntegrationType, frozenset[str]] = {
    IntegrationType.GMAIL: frozenset(get_google_required_granted_scopes(IntegrationType.GMAIL)),
    IntegrationType.GOOGLE_CALENDAR: frozenset(
        get_google_required_granted_scopes(IntegrationType.GOOGLE_CALENDAR)
    ),
    IntegrationType.OUTLOOK_EMAIL: frozenset(
        scope
        for scope in MICROSOFT_INTEGRATION_SCOPES[IntegrationType.OUTLOOK_EMAIL]
        if scope != MICROSOFT_SCOPE_OFFLINE_ACCESS
    ),
    IntegrationType.OUTLOOK_CALENDAR: frozenset(
        scope
        for scope in MICROSOFT_INTEGRATION_SCOPES[IntegrationType.OUTLOOK_CALENDAR]
        if scope != MICROSOFT_SCOPE_OFFLINE_ACCESS
    ),
    IntegrationType.TEAMS: frozenset(
        scope
        for scope in MICROSOFT_INTEGRATION_SCOPES[IntegrationType.TEAMS]
        if scope != MICROSOFT_SCOPE_OFFLINE_ACCESS
    ),
    IntegrationType.SLACK: frozenset(SLACK_USER_SCOPES),
    IntegrationType.HUBSPOT: frozenset(
        {
            "oauth",
            "crm.objects.companies.read",
            "crm.objects.contacts.read",
            "crm.objects.deals.read",
            "crm.objects.owners.read",
            "crm.schemas.deals.read",
        }
    ),
    IntegrationType.SALESFORCE: frozenset(
        {
            "refresh_token",
            "api",
        }
    ),
}

_REQUIRED_TOKEN_FIELDS: dict[IntegrationType, frozenset[str]] = {
    IntegrationType.GMAIL: frozenset({"refresh_token"}),
    IntegrationType.GOOGLE_CALENDAR: frozenset({"refresh_token"}),
    IntegrationType.OUTLOOK_EMAIL: frozenset({"refresh_token"}),
    IntegrationType.OUTLOOK_CALENDAR: frozenset({"refresh_token"}),
    IntegrationType.TEAMS: frozenset({"refresh_token"}),
}

# A required scope may be satisfied by a broader grant.
# This map lets us keep required checks strict while recognizing common aliasing.
_SATISFYING_SCOPES: dict[str, frozenset[str]] = {
    GOOGLE_SCOPE_GMAIL_READONLY: frozenset(
        {
            GOOGLE_SCOPE_GMAIL_READONLY,
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        }
    ),
    GOOGLE_SCOPE_GMAIL_SEND: frozenset(
        {
            GOOGLE_SCOPE_GMAIL_SEND,
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        }
    ),
    GOOGLE_SCOPE_CALENDAR_READONLY: frozenset(
        {
            GOOGLE_SCOPE_CALENDAR_READONLY,
            "https://www.googleapis.com/auth/calendar",
        }
    ),
    GOOGLE_SCOPE_CALENDAR_EVENTS: frozenset(
        {
            GOOGLE_SCOPE_CALENDAR_EVENTS,
            "https://www.googleapis.com/auth/calendar",
        }
    ),
    "refresh_token": frozenset(
        {
            "refresh_token",
            "offline_access",
        }
    ),
    MICROSOFT_SCOPE_USER_READ: frozenset(
        {
            MICROSOFT_SCOPE_USER_READ,
            MICROSOFT_SCOPE_USER_READ_ALL,
        }
    ),
    MICROSOFT_SCOPE_CALENDARS_READ_WRITE: frozenset(
        {
            MICROSOFT_SCOPE_CALENDARS_READ_WRITE,
        }
    ),
    MICROSOFT_SCOPE_MAIL_SEND: frozenset(
        {
            MICROSOFT_SCOPE_MAIL_SEND,
        }
    ),
}

_SCOPE_DISPLAY_LABELS: dict[str, str] = {
    GOOGLE_SCOPE_GMAIL_READONLY: "Gmail access",
    GOOGLE_SCOPE_GMAIL_SEND: "Gmail send access",
    GOOGLE_SCOPE_CALENDAR_READONLY: "Google Calendar access",
    GOOGLE_SCOPE_CALENDAR_EVENTS: "Google Calendar invite access",
    GOOGLE_SCOPE_MEETINGS_SPACE_READONLY: "Google Meet access",
    "channels:read": "Slack channel access",
    "groups:read": "Slack private channel access",
    "mpim:read": "Slack direct message access",
    "channels:history": "Slack channel history access",
    "groups:history": "Slack private channel history access",
    "mpim:history": "Slack direct message history access",
    "users:read": "Slack user access",
    "users:read.email": "Slack user email access",
    "team:read": "Slack workspace access",
    "oauth": "HubSpot app access",
    "api": "Salesforce API access",
    "refresh_token": "Salesforce offline access",
    "Mail.Read": "Outlook mail access",
    "Mail.Send": "Outlook mail send access",
    "Calendars.Read": "Outlook calendar access",
    "Calendars.ReadWrite": "Outlook calendar write access",
    "Chat.Read": "Microsoft Teams chat access",
    "ChannelMessage.Read.All": "Microsoft Teams channel message access",
    "Channel.ReadBasic.All": "Microsoft Teams channel access",
    "ChannelSettings.Read.All": "Microsoft Teams channel settings access",
    "ChannelSettings.ReadWrite.All": "Microsoft Teams channel settings access",
    "Team.ReadBasic.All": "Microsoft Teams team access",
    "User.Read": "Microsoft profile access",
    "User.Read.All": "Microsoft user directory access",
    "Group.Read.All": "Microsoft group access",
    "Directory.Read.All": "Microsoft directory access",
    "Directory.ReadWrite.All": "Microsoft directory access",
    "Group.ReadWrite.All": "Microsoft group access",
    "OnlineMeetingTranscript.Read.All": "Online meeting transcript access",
    "OnlineMeetings.Read": "Online meetings access",
}


def get_required_scopes(integration_type: IntegrationType) -> frozenset[str]:
    """Return the required scopes for an integration type."""
    # Unknown integration types intentionally map to empty scope set.
    # Callers can keep behavior explicit instead of crashing at lookup time.
    return _REQUIRED_SCOPES.get(integration_type, frozenset())


def get_required_token_fields(integration_type: IntegrationType) -> frozenset[str]:
    return _REQUIRED_TOKEN_FIELDS.get(integration_type, frozenset())


def parse_granted_scopes(
    scope_value: str | list[str] | None,
    fallback: list[str] | None = None,
) -> list[str] | None:
    """Parse a provider-provided scope field into a normalized scope list."""
    # Normalizing mixed delimiters lets downstream checks treat provider payloads
    # uniformly regardless of response format.
    if isinstance(scope_value, str):
        parsed = [scope for scope in re.split(r"[\s,]+", scope_value) if scope]
        return parsed or fallback
    if isinstance(scope_value, list):
        parsed = [scope for scope in scope_value if isinstance(scope, str) and scope]
        return parsed or fallback
    return fallback


def parse_granted_scopes_from_token_payload(
    token_payload: dict[str, object],
    fallback: list[str] | None = None,
) -> list[str] | None:
    """Parse `scope` or `scopes` from token payloads."""
    # Provider token formats vary; normalize both keys here for a single contract.
    scope_value = token_payload.get("scope", token_payload.get("scopes"))
    if isinstance(scope_value, (str, list)) or scope_value is None:
        return parse_granted_scopes(scope_value, fallback)
    return fallback


def get_missing_required_scopes(
    integration_type: IntegrationType,
    granted_scopes: list[str] | None,
) -> set[str]:
    """Return required scopes that were not satisfied by granted scopes."""
    # A required scope can be met by a broader scope set; this check keeps that
    # allowance centralized and deterministic.
    required_scopes = get_required_scopes(integration_type)
    if not required_scopes:
        return set()

    granted_scope_set = set(granted_scopes or [])
    missing_scopes: set[str] = set()

    for required_scope in required_scopes:
        satisfying_scopes = _SATISFYING_SCOPES.get(required_scope, frozenset({required_scope}))
        if granted_scope_set.isdisjoint(satisfying_scopes):
            missing_scopes.add(required_scope)

    return missing_scopes


def is_required_scope_satisfied(required_scope: str, granted_scopes: list[str] | None) -> bool:
    satisfying_scopes = _SATISFYING_SCOPES.get(required_scope, frozenset({required_scope}))
    return not set(granted_scopes or []).isdisjoint(satisfying_scopes)


def get_missing_required_token_fields(
    integration_type: IntegrationType,
    token_payload: dict[str, object],
) -> set[str]:
    missing_fields: set[str] = set()
    for field_name in get_required_token_fields(integration_type):
        field_value = token_payload.get(field_name)
        if not isinstance(field_value, str) or not field_value:
            missing_fields.add(field_name)
    return missing_fields


def is_microsoft_integration(integration_type: IntegrationType) -> bool:
    return integration_type in _MICROSOFT_INTEGRATION_TYPES


def is_google_integration(integration_type: IntegrationType) -> bool:
    return integration_type in _GOOGLE_INTEGRATION_TYPES


def get_scope_display_label(scope: str) -> str:
    """Return a UI-friendly scope label with a graceful fallback."""
    return _SCOPE_DISPLAY_LABELS.get(scope, scope)


def get_missing_scope_labels(missing_scopes: set[str]) -> list[str]:
    """Map missing scopes to UI labels in deterministic order."""
    return [get_scope_display_label(scope) for scope in sorted(missing_scopes)]
