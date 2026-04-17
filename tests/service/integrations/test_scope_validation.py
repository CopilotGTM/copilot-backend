"""Tests for shared scope parsing and missing-scope detection helpers."""

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.scope_validation import (
    get_missing_required_scopes,
    parse_granted_scopes,
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
