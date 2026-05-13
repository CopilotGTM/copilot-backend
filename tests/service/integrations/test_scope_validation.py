"""Tests for shared scope parsing and missing-scope detection helpers."""

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.scope_validation import (
    get_missing_required_scopes,
    get_missing_required_token_fields,
    parse_granted_scopes,
    parse_granted_scopes_from_token_payload,
)


def test_parse_granted_scopes_supports_space_delimited_strings() -> None:
    """Parse spaces-separated scope strings from OAuth token fields."""
    # Contract: space-delimited scope strings from OAuth providers parse correctly.
    assert parse_granted_scopes(
        "https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/gmail.readonly"
    ) == [
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]


def test_parse_granted_scopes_supports_comma_delimited_strings() -> None:
    """Parse comma-separated scope strings from token responses."""
    # Contract: comma-delimited scope strings are parsed as separate scopes.
    assert parse_granted_scopes("channels:read,groups:read,users:read.email") == [
        "channels:read",
        "groups:read",
        "users:read.email",
    ]


def test_parse_granted_scopes_from_token_payload_supports_hubspot_scopes_array() -> None:
    """Handle HubSpot scope arrays and return a normalized grant list."""
    # Contract: token payload arrays map to the same normalized grant list.
    assert parse_granted_scopes_from_token_payload(
        {"scopes": ["oauth", "crm.objects.deals.read"]},
    ) == ["oauth", "crm.objects.deals.read"]


def test_get_missing_required_scopes_detects_missing_google_meet_scope() -> None:
    """Google Calendar requires meetings scope in addition to calendar read."""
    # Contract: Google Calendar requires both calendar readonly and meetings scope.
    assert get_missing_required_scopes(
        IntegrationType.GOOGLE_CALENDAR,
        [
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
    ) == {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/meetings.space.readonly",
    }


def test_google_calendar_requires_event_write_scope_for_invites() -> None:
    """Google Calendar invite sending requires event-write permission."""
    assert get_missing_required_scopes(
        IntegrationType.GOOGLE_CALENDAR,
        [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/meetings.space.readonly",
        ],
    ) == {"https://www.googleapis.com/auth/calendar.events"}


def test_get_missing_required_scopes_accepts_broader_gmail_scopes() -> None:
    """Accept Gmail aliases that cover readonly behavior."""
    # Contract: broader Gmail scopes satisfy narrower readonly requirement.
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
    """Reject non-equivalent Gmail scopes that do not satisfy readonly."""
    # Contract: gmail.modify alone must not satisfy readonly-required integration.
    assert get_missing_required_scopes(
        IntegrationType.GMAIL,
        [
            "https://www.googleapis.com/auth/gmail.modify",
        ],
    ) == {"https://www.googleapis.com/auth/gmail.readonly"}


def test_get_missing_required_scopes_accepts_broader_google_calendar_scope() -> None:
    """Google full calendar scope should satisfy read and event-write requirements."""
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
    """Return all missing Slack scopes required for read/write workflows."""
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
    """HubSpot deals schema scope should remain required for CRM write paths."""
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
    """Salesforce token checks should treat offline_access as refresh_token alias."""
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


def test_microsoft_outlook_email_missing_scope_detection_matches_read_and_send_flow() -> None:
    """Outlook Mail integration requires read plus send permissions."""
    assert get_missing_required_scopes(
        IntegrationType.OUTLOOK_EMAIL,
        [
            "User.Read",
            "Mail.Read",
        ],
    ) == {"Mail.Send"}


def test_microsoft_outlook_email_accepts_mail_send_scope_for_send_tool() -> None:
    """Mail.Send should satisfy Outlook email send prerequisites."""
    assert (
        get_missing_required_scopes(
            IntegrationType.OUTLOOK_EMAIL,
            [
                "User.Read",
                "Mail.Read",
                "Mail.Send",
            ],
        )
        == set()
    )


def test_microsoft_outlook_calendar_requires_write_scope_for_invites() -> None:
    """Outlook Calendar invites require write-level calendar permission."""
    assert get_missing_required_scopes(
        IntegrationType.OUTLOOK_CALENDAR,
        [
            "offline_access",
            "User.Read",
            "Calendars.Read",
            "OnlineMeetings.Read",
        ],
    ) == {"Calendars.ReadWrite"}


def test_microsoft_outlook_calendar_accepts_readwrite_scope_for_invites() -> None:
    """Read-write calendar scope should satisfy invite send prerequisites."""
    assert (
        get_missing_required_scopes(
            IntegrationType.OUTLOOK_CALENDAR,
            [
                "User.Read",
                "Calendars.ReadWrite",
                "OnlineMeetings.Read",
            ],
        )
        == set()
    )


def test_microsoft_user_read_all_satisfies_user_read_profile_requirement() -> None:
    """Prefer stronger Microsoft user profile grants when available."""
    assert (
        get_missing_required_scopes(
            IntegrationType.OUTLOOK_EMAIL,
            [
                "User.Read.All",
                "Mail.Read",
                "Mail.Send",
            ],
        )
        == set()
    )


def test_microsoft_teams_no_longer_requires_directory_group_or_channel_settings_scopes() -> None:
    """Current Teams flow should not force legacy directory/group scope requirements."""
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
                "Mail.Send",
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
