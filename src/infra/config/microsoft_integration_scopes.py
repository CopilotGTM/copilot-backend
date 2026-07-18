"""Microsoft Graph scope sets for CopilotGTM user-owned integrations.

These constants are the canonical source for scope comparison during validation.
Only the scopes in this map are considered required for new feature behavior
that depends on user-consented Microsoft Graph permissions.
"""

from __future__ import annotations

import hashlib

from src.domain.models.organisation_tables.sync import IntegrationType

MICROSOFT_SCOPE_OFFLINE_ACCESS = "offline_access"
MICROSOFT_SCOPE_USER_READ = "User.Read"
MICROSOFT_SCOPE_USER_READ_ALL = "User.Read.All"
MICROSOFT_SCOPE_CALENDARS_READ_WRITE = "Calendars.ReadWrite"
MICROSOFT_SCOPE_MAIL_SEND = "Mail.Send"
MICROSOFT_SCOPE_MAIL_READ_WRITE = "Mail.ReadWrite"
MICROSOFT_SCOPE_CHANNEL_MESSAGE_READ_ALL = "ChannelMessage.Read.All"
MICROSOFT_SCOPE_ONLINE_MEETINGS_READ = "OnlineMeetings.Read"
MICROSOFT_SCOPE_ONLINE_MEETING_TRANSCRIPT_READ_ALL = "OnlineMeetingTranscript.Read.All"

MICROSOFT_INTEGRATION_SCOPES: dict[IntegrationType, tuple[str, ...]] = {
    IntegrationType.MICROSOFT: (
        MICROSOFT_SCOPE_OFFLINE_ACCESS,
        MICROSOFT_SCOPE_USER_READ,
        "Mail.Read",
        MICROSOFT_SCOPE_MAIL_READ_WRITE,
        MICROSOFT_SCOPE_MAIL_SEND,
        MICROSOFT_SCOPE_CALENDARS_READ_WRITE,
        MICROSOFT_SCOPE_ONLINE_MEETINGS_READ,
    ),
    IntegrationType.OUTLOOK_EMAIL: (
        MICROSOFT_SCOPE_OFFLINE_ACCESS,
        MICROSOFT_SCOPE_USER_READ,
        "Mail.Read",
        MICROSOFT_SCOPE_MAIL_READ_WRITE,
        MICROSOFT_SCOPE_MAIL_SEND,
    ),
    IntegrationType.OUTLOOK_CALENDAR: (
        MICROSOFT_SCOPE_OFFLINE_ACCESS,
        MICROSOFT_SCOPE_USER_READ,
        MICROSOFT_SCOPE_CALENDARS_READ_WRITE,
        MICROSOFT_SCOPE_ONLINE_MEETINGS_READ,
    ),
    IntegrationType.TEAMS: (
        MICROSOFT_SCOPE_OFFLINE_ACCESS,
        MICROSOFT_SCOPE_USER_READ,
        "Team.ReadBasic.All",
        "Channel.ReadBasic.All",
        "Chat.Read",
    ),
}


def get_microsoft_integration_scopes(integration_type: IntegrationType) -> tuple[str, ...]:
    """Return configured scopes for an integration type.

    Returns an empty tuple when the integration type is not mapped here, which
    keeps callers defensive and avoids forcing unknown integrations into scope
    validation failures.
    """
    return MICROSOFT_INTEGRATION_SCOPES.get(integration_type, ())


def get_microsoft_admin_consent_scopes() -> tuple[str, ...]:
    """Return the org-level Microsoft consent bundle for all user-owned integrations."""
    seen: set[str] = set()
    scopes: list[str] = []
    for integration_type in (
        IntegrationType.OUTLOOK_EMAIL,
        IntegrationType.OUTLOOK_CALENDAR,
        IntegrationType.TEAMS,
    ):
        for scope in get_microsoft_integration_scopes(integration_type):
            if scope in seen:
                continue
            seen.add(scope)
            scopes.append(scope)
    return tuple(scopes)


def get_microsoft_admin_consent_scope_version() -> str:
    """Stable version for detecting stale Microsoft admin consent after scope changes."""
    scope_blob = "\n".join(get_microsoft_admin_consent_scopes()).encode("utf-8")
    return hashlib.sha256(scope_blob).hexdigest()[:12]


def normalize_microsoft_scope(scope: str) -> str:
    prefix = "https://graph.microsoft.com/"
    return scope.removeprefix(prefix)


def parse_microsoft_scope_string(scope: str | None) -> list[str]:
    if not scope:
        return []
    scopes: list[str] = []
    seen: set[str] = set()
    for raw_scope in scope.replace(",", " ").split():
        normalized = normalize_microsoft_scope(raw_scope.strip())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        scopes.append(normalized)
    return scopes
