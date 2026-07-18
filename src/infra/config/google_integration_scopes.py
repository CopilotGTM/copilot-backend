"""Google OAuth scope sets for CopilotGTM user-owned integrations."""

from __future__ import annotations

from src.domain.models.organisation_tables.sync import IntegrationType

GOOGLE_SCOPE_USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
GOOGLE_SCOPE_USERINFO_PROFILE = "https://www.googleapis.com/auth/userinfo.profile"
GOOGLE_SCOPE_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_SCOPE_GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_SCOPE_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_SCOPE_CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_SCOPE_CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_SCOPE_MEETINGS_SPACE_READONLY = "https://www.googleapis.com/auth/meetings.space.readonly"

GOOGLE_IDENTITY_SCOPES = (
    GOOGLE_SCOPE_USERINFO_EMAIL,
    GOOGLE_SCOPE_USERINFO_PROFILE,
)
GOOGLE_CALENDAR_SCOPES = (
    *GOOGLE_IDENTITY_SCOPES,
    GOOGLE_SCOPE_CALENDAR_READONLY,
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_MEETINGS_SPACE_READONLY,
)
GOOGLE_CALENDAR_REQUIRED_GRANTED_SCOPES = (
    GOOGLE_SCOPE_CALENDAR_READONLY,
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_MEETINGS_SPACE_READONLY,
)
GOOGLE_PROVIDER_SCOPES_WITHOUT_READ = (
    *GOOGLE_CALENDAR_SCOPES,
    GOOGLE_SCOPE_GMAIL_SEND,
)
GOOGLE_PROVIDER_SCOPES = (
    *GOOGLE_CALENDAR_SCOPES,
    GOOGLE_SCOPE_GMAIL_READONLY,
    GOOGLE_SCOPE_GMAIL_SEND,
    GOOGLE_SCOPE_GMAIL_COMPOSE,
)

GOOGLE_INTEGRATION_SCOPES: dict[IntegrationType, tuple[str, ...]] = {
    IntegrationType.GOOGLE: GOOGLE_PROVIDER_SCOPES,
    IntegrationType.GMAIL: (
        *GOOGLE_IDENTITY_SCOPES,
        GOOGLE_SCOPE_GMAIL_READONLY,
        GOOGLE_SCOPE_GMAIL_SEND,
        GOOGLE_SCOPE_GMAIL_COMPOSE,
    ),
    IntegrationType.GOOGLE_CALENDAR: GOOGLE_CALENDAR_SCOPES,
}

GOOGLE_REQUIRED_GRANTED_SCOPES: dict[IntegrationType, tuple[str, ...]] = {
    IntegrationType.GOOGLE: GOOGLE_CALENDAR_REQUIRED_GRANTED_SCOPES,
    IntegrationType.GMAIL: (GOOGLE_SCOPE_GMAIL_SEND,),
    IntegrationType.GOOGLE_CALENDAR: GOOGLE_CALENDAR_REQUIRED_GRANTED_SCOPES,
}


def get_google_integration_scopes(
    integration_type: IntegrationType,
    *,
    include_gmail_read_scope: bool = True,
) -> tuple[str, ...]:
    if integration_type == IntegrationType.GOOGLE and not include_gmail_read_scope:
        return GOOGLE_PROVIDER_SCOPES_WITHOUT_READ
    return GOOGLE_INTEGRATION_SCOPES.get(integration_type, ())


def get_google_required_granted_scopes(integration_type: IntegrationType) -> tuple[str, ...]:
    return GOOGLE_REQUIRED_GRANTED_SCOPES.get(integration_type, ())
