"""Shared helpers for parsing granted scopes and validating required permissions."""

from __future__ import annotations

import re

from src.domain.models.organisation_tables.sync import IntegrationType

_REQUIRED_SCOPES: dict[IntegrationType, frozenset[str]] = {
    IntegrationType.GMAIL: frozenset(
        {
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    ),
    IntegrationType.GOOGLE_CALENDAR: frozenset(
        {
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/meetings.space.readonly",
        }
    ),
    IntegrationType.OUTLOOK_EMAIL: frozenset(
        {
            "Mail.Read",
            "Calendars.Read",
            "Chat.Read",
            "ChannelMessage.Read.All",
            "Team.ReadBasic.All",
            "User.Read.All",
        }
    ),
    IntegrationType.OUTLOOK_CALENDAR: frozenset(
        {
            "Mail.Read",
            "Calendars.Read",
            "Chat.Read",
            "User.Read.All",
            "OnlineMeetingTranscript.Read.All",
            "OnlineMeetings.Read",
        }
    ),
    IntegrationType.TEAMS: frozenset(
        {
            "Team.ReadBasic.All",
            "Chat.Read",
            "ChannelMessage.Read.All",
            "Channel.ReadBasic.All",
            "ChannelSettings.Read.All",
            "ChannelSettings.ReadWrite.All",
            "Group.Read.All",
            "Directory.Read.All",
            "Directory.ReadWrite.All",
            "Group.ReadWrite.All",
        }
    ),
    IntegrationType.SLACK: frozenset(
        {
            "channels:read",
            "groups:read",
            "mpim:read",
            "channels:history",
            "groups:history",
            "mpim:history",
            "users:read",
            "users:read.email",
            "team:read",
        }
    ),
    IntegrationType.HUBSPOT: frozenset(
        {
            "oauth",
            "automation",
            "conversations.read",
            "sales-email-read",
            "crm.objects.companies.read",
            "crm.objects.contacts.read",
            "crm.objects.deals.read",
            "crm.objects.leads.read",
            "crm.objects.line_items.read",
            "crm.objects.products.read",
            "crm.objects.owners.read",
            "crm.objects.users.read",
            "crm.objects.tasks.read",
            "crm.objects.notes.read",
            "crm.schemas.companies.read",
            "crm.schemas.contacts.read",
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

_SATISFYING_SCOPES: dict[str, frozenset[str]] = {
    # Google can return a previously granted broader scope when
    # include_granted_scopes=true. Treat those broader grants as satisfying the
    # narrower readonly requirement we enforce at connect time.
    "https://www.googleapis.com/auth/gmail.readonly": frozenset(
        {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://mail.google.com/",
        }
    ),
    "https://www.googleapis.com/auth/calendar.readonly": frozenset(
        {
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar",
        }
    ),
    "refresh_token": frozenset(
        {
            "refresh_token",
            "offline_access",
        }
    ),
}

_SCOPE_DISPLAY_LABELS: dict[str, str] = {
    "https://www.googleapis.com/auth/gmail.readonly": "Gmail access",
    "https://www.googleapis.com/auth/calendar.readonly": "Google Calendar access",
    "https://www.googleapis.com/auth/meetings.space.readonly": "Google Meet access",
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
    "automation": "HubSpot automation access",
    "conversations.read": "HubSpot conversations access",
    "sales-email-read": "HubSpot sales email access",
    "crm.objects.tasks.read": "HubSpot task access",
    "crm.objects.notes.read": "HubSpot note access",
    "api": "Salesforce API access",
    "refresh_token": "Salesforce offline access",
    "Mail.Read": "Outlook mail access",
    "Calendars.Read": "Outlook calendar access",
    "Chat.Read": "Microsoft Teams chat access",
    "ChannelMessage.Read.All": "Microsoft Teams channel message access",
    "Channel.ReadBasic.All": "Microsoft Teams channel access",
    "ChannelSettings.Read.All": "Microsoft Teams channel settings access",
    "ChannelSettings.ReadWrite.All": "Microsoft Teams channel settings access",
    "Team.ReadBasic.All": "Microsoft Teams team access",
    "User.Read.All": "Microsoft user directory access",
    "Group.Read.All": "Microsoft group access",
    "Directory.Read.All": "Microsoft directory access",
    "Directory.ReadWrite.All": "Microsoft directory access",
    "Group.ReadWrite.All": "Microsoft group access",
    "OnlineMeetingTranscript.Read.All": "Online meeting transcript access",
    "OnlineMeetings.Read": "Online meetings access",
}


def get_required_scopes(integration_type: IntegrationType) -> frozenset[str]:
    return _REQUIRED_SCOPES.get(integration_type, frozenset())


def parse_granted_scopes(
    scope_value: str | list[str] | None,
    fallback: list[str] | None = None,
) -> list[str] | None:
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
    scope_value = token_payload.get("scope")
    if isinstance(scope_value, (str, list)) or scope_value is None:
        return parse_granted_scopes(scope_value, fallback)
    return fallback


def get_missing_required_scopes(
    integration_type: IntegrationType,
    granted_scopes: list[str] | None,
) -> set[str]:
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


def get_scope_display_label(scope: str) -> str:
    return _SCOPE_DISPLAY_LABELS.get(scope, scope)


def get_missing_scope_labels(missing_scopes: set[str]) -> list[str]:
    return [get_scope_display_label(scope) for scope in sorted(missing_scopes)]
