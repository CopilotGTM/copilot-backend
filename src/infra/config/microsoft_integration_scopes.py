"""Microsoft Graph scope sets for CopilotGTM user-owned integrations."""

from __future__ import annotations

from src.domain.models.organisation_tables.sync import IntegrationType

MICROSOFT_SCOPE_OFFLINE_ACCESS = "offline_access"
MICROSOFT_SCOPE_USER_READ = "User.Read"
MICROSOFT_SCOPE_USER_READ_ALL = "User.Read.All"
MICROSOFT_SCOPE_CHANNEL_MESSAGE_READ_ALL = "ChannelMessage.Read.All"
MICROSOFT_SCOPE_ONLINE_MEETINGS_READ = "OnlineMeetings.Read"
MICROSOFT_SCOPE_ONLINE_MEETING_TRANSCRIPT_READ_ALL = "OnlineMeetingTranscript.Read.All"

MICROSOFT_INTEGRATION_SCOPES: dict[IntegrationType, tuple[str, ...]] = {
    IntegrationType.OUTLOOK_EMAIL: (
        MICROSOFT_SCOPE_OFFLINE_ACCESS,
        MICROSOFT_SCOPE_USER_READ,
        "Mail.Read",
    ),
    IntegrationType.OUTLOOK_CALENDAR: (
        MICROSOFT_SCOPE_OFFLINE_ACCESS,
        MICROSOFT_SCOPE_USER_READ,
        "Calendars.Read",
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
    return MICROSOFT_INTEGRATION_SCOPES.get(integration_type, ())
