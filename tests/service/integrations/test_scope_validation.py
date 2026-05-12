"""Tests for shared scope parsing and missing-scope detection helpers."""

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.scope_validation import (
    get_missing_required_scopes,
    get_missing_required_token_fields,
    parse_granted_scopes,
    parse_granted_scopes_from_token_payload,
)


def test_parse_granted_scopes_supports_space_delimited_strings() -> None:
    assert parse_granted_scopes(
        "https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/gmail.readonly"
    ) == [
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]


def test_parse_granted_scopes_supports_comma_delimited_strings() -> None:
    assert parse_granted_scopes("channels:read,groups:read,users:read.email") == [
        "channels:read",
        "groups:read",
        "users:read.email",
    ]


def test_parse_granted_scopes_from_token_payload_supports_hubspot_scopes_array() -> None:
    assert parse_granted_scopes_from_token_payload(
        {"scopes": ["oauth", "crm.objects.deals.read"]},
    ) == ["oauth", "crm.objects.deals.read"]


def test_get_missing_required_scopes_detects_missing_google_meet_scope() -> None:
    assert get_missing_required_scopes(
        IntegrationType.GOOGLE_CALENDAR,
        [
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
    ) == {"https://www.googleapis.com/auth/meetings.space.readonly"}


def test_get_missing_required_scopes_accepts_broader_gmail_scopes() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.GMAIL,
            [
                "https://mail.google.com/",
            ],
        )
        == set()
    )


def test_get_missing_required_scopes_rejects_gmail_modify_for_readonly_integration() -> None:
    assert get_missing_required_scopes(
        IntegrationType.GMAIL,
        [
            "https://www.googleapis.com/auth/gmail.modify",
        ],
    ) == {"https://www.googleapis.com/auth/gmail.readonly"}


def test_get_missing_required_scopes_accepts_broader_google_calendar_scope() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.GOOGLE_CALENDAR,
            [
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/meetings.space.readonly",
            ],
        )
        == set()
    )


def test_get_missing_required_scopes_detects_missing_slack_scopes() -> None:
    assert get_missing_required_scopes(
        IntegrationType.SLACK,
        [
            "channels:read",
            "groups:read",
        ],
    ) == {
        "mpim:read",
        "channels:history",
        "groups:history",
        "mpim:history",
        "users:read",
        "users:read.email",
        "team:read",
    }


def test_get_missing_required_scopes_detects_missing_hubspot_deal_schema_scope() -> None:
    assert "crm.schemas.deals.read" in get_missing_required_scopes(
        IntegrationType.HUBSPOT,
        [
            "oauth",
            "crm.objects.companies.read",
            "crm.objects.contacts.read",
            "crm.objects.deals.read",
            "crm.objects.owners.read",
        ],
    )


def test_get_missing_required_scopes_accepts_salesforce_offline_access_alias() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.SALESFORCE,
            [
                "api",
                "offline_access",
            ],
        )
        == set()
    )


def test_microsoft_outlook_email_missing_scope_detection_matches_mail_only_flow() -> None:
    assert get_missing_required_scopes(
        IntegrationType.OUTLOOK_EMAIL,
        [
            "User.Read",
        ],
    ) == {"Mail.Read"}


def test_microsoft_outlook_calendar_does_not_require_transcript_scopes_for_connect() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.OUTLOOK_CALENDAR,
            [
                "User.Read",
                "Calendars.Read",
                "OnlineMeetings.Read",
            ],
        )
        == set()
    )


def test_microsoft_user_read_all_satisfies_user_read_profile_requirement() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.OUTLOOK_EMAIL,
            [
                "User.Read.All",
                "Mail.Read",
            ],
        )
        == set()
    )


def test_microsoft_teams_no_longer_requires_directory_group_or_channel_settings_scopes() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.TEAMS,
            [
                "User.Read",
                "Team.ReadBasic.All",
                "Channel.ReadBasic.All",
                "Chat.Read",
            ],
        )
        == set()
    )


def test_microsoft_offline_access_is_validated_by_refresh_token_not_scope_echo() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.OUTLOOK_EMAIL,
            [
                "User.Read",
                "Mail.Read",
            ],
        )
        == set()
    )
    assert get_missing_required_token_fields(
        IntegrationType.OUTLOOK_EMAIL,
        {"access_token": "access-token"},
    ) == {"refresh_token"}
    assert (
        get_missing_required_token_fields(
            IntegrationType.OUTLOOK_EMAIL,
            {"access_token": "access-token", "refresh_token": "refresh-token"},
        )
        == set()
    )


def test_google_offline_access_is_validated_by_refresh_token_not_scope_echo() -> None:
    assert (
        get_missing_required_scopes(
            IntegrationType.GMAIL,
            [
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
        )
        == set()
    )
    assert get_missing_required_token_fields(
        IntegrationType.GMAIL,
        {"access_token": "access-token"},
    ) == {"refresh_token"}
