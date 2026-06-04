"""Tests for callback-scope validation helpers."""

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.copilot_callback_service import CopilotCallbackService


def test_validate_required_scopes_allows_granted_gmail_scope() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.GMAIL,
        {
            "scope": "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    )


def test_validate_required_scopes_allows_send_only_gmail_scope() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.GMAIL,
        {
            "scope": "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/userinfo.profile "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    )


def test_validate_required_scopes_rejects_missing_gmail_scope() -> None:
    with pytest.raises(ValueError, match="Gmail permission was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.GMAIL,
            {
                "scope": "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/userinfo.profile"
            },
        )


def test_validate_required_scopes_rejects_missing_google_meet_scope() -> None:
    with pytest.raises(ValueError, match="Google Meet permission was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.GOOGLE_CALENDAR,
            {
                "scope": "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/calendar.readonly"
            },
        )


def test_validate_required_scopes_rejects_missing_google_calendar_events_scope() -> None:
    with pytest.raises(ValueError, match="Google Calendar invite permission was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.GOOGLE_CALENDAR,
            {
                "scope": "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/calendar.readonly "
                "https://www.googleapis.com/auth/meetings.space.readonly",
                "refresh_token": "refresh-token",
            },
        )


def test_validate_required_scopes_rejects_missing_hubspot_deal_schema_permission() -> None:
    with pytest.raises(ValueError, match="HubSpot permissions were not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.HUBSPOT,
            {
                "scope": "oauth crm.objects.companies.read crm.objects.contacts.read "
                "crm.objects.deals.read crm.objects.owners.read"
            },
        )


def test_validate_required_scopes_accepts_salesforce_offline_access_scope() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.SALESFORCE,
        {
            "scope": "api offline_access openid profile email",
        },
    )


def test_validate_required_scopes_accepts_microsoft_without_offline_scope_echo() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.OUTLOOK_EMAIL,
        {
            "scope": "User.Read Mail.Read Mail.Send",
            "refresh_token": "refresh-token",
        },
    )


def test_validate_required_scopes_rejects_microsoft_without_refresh_token() -> None:
    with pytest.raises(ValueError, match="Microsoft offline access was not granted"):
        CopilotCallbackService()._validate_required_scopes(
            IntegrationType.OUTLOOK_EMAIL,
            {
                "scope": "User.Read Mail.Read Mail.Send",
            },
        )


def test_validate_required_scopes_accepts_google_without_offline_scope_echo() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.GMAIL,
        {
            "scope": "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    )


def test_google_parent_default_requires_calendar_but_not_gmail() -> None:
    service = CopilotCallbackService()

    service._validate_required_scopes(
        IntegrationType.GOOGLE,
        {
            "scope": "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/meetings.space.readonly",
            "refresh_token": "refresh-token",
        },
    )

    assert service._get_enabled_capabilities(
        IntegrationType.GOOGLE,
        {
            "scope": "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/meetings.space.readonly",
            "refresh_token": "refresh-token",
        },
    ) == [IntegrationType.GOOGLE_CALENDAR]


def test_google_parent_enables_gmail_when_gmail_scopes_are_granted() -> None:
    assert CopilotCallbackService()._get_enabled_capabilities(
        IntegrationType.GOOGLE,
        {
            "scope": "https://www.googleapis.com/auth/calendar.readonly "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/meetings.space.readonly "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send",
            "refresh_token": "refresh-token",
        },
    ) == [IntegrationType.GOOGLE_CALENDAR, IntegrationType.GMAIL]


def test_microsoft_parent_default_enables_email_and_calendar_without_teams() -> None:
    assert CopilotCallbackService()._get_enabled_capabilities(
        IntegrationType.MICROSOFT,
        {
            "scope": "User.Read Mail.Read Mail.Send Calendars.ReadWrite OnlineMeetings.Read",
            "refresh_token": "refresh-token",
        },
    ) == [IntegrationType.OUTLOOK_EMAIL, IntegrationType.OUTLOOK_CALENDAR]
