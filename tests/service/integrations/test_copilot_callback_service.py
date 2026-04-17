"""Tests for callback-scope validation helpers."""

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.copilot_callback_service import CopilotCallbackService


def test_validate_required_scopes_allows_granted_gmail_scope() -> None:
    CopilotCallbackService()._validate_required_scopes(
        IntegrationType.GMAIL,
        {
            "scope": "https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/gmail.readonly"
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
