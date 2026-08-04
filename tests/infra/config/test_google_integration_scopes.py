from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.google_integration_scopes import (
    GOOGLE_REQUIRED_GRANTED_SCOPES,
    get_google_integration_scopes,
)


def test_google_provider_required_scopes_match_calendar_capability() -> None:
    assert (
        GOOGLE_REQUIRED_GRANTED_SCOPES[IntegrationType.GOOGLE]
        is GOOGLE_REQUIRED_GRANTED_SCOPES[IntegrationType.GOOGLE_CALENDAR]
    )


def test_google_parent_scopes_exclude_gmail_read_scope_when_disabled() -> None:
    assert get_google_integration_scopes(
        IntegrationType.GOOGLE,
        include_gmail_read_scope=False,
    ) == (
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    )


def test_google_parent_scopes_include_gmail_read_scope_when_enabled() -> None:
    assert get_google_integration_scopes(
        IntegrationType.GOOGLE,
        include_gmail_read_scope=True,
    ) == (
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
    )
