"""Regression tests for Google webhook metadata shape.

The metadata helpers are shared by subscription creation and webhook
validation, so these tests lock the JSON contract stored on KnowledgeIntegration.
"""

from datetime import UTC, datetime

from src.service.integrations.webhooks.google.metadata import (
    GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY,
    GoogleSubscriptionKind,
    get_subscription_metadata,
    set_calendar_subscription_metadata,
    set_gmail_subscription_metadata,
)


def test_set_and_get_gmail_subscription_metadata_preserves_existing_values() -> None:
    """Gmail watch writes must not clobber unrelated connection metadata."""
    updated = set_gmail_subscription_metadata(
        {"existing": {"value": 1}},
        watch_history_id="123",
        topic_name="projects/proj/topics/gmail",
        label_ids=["INBOX"],
        label_filter_behavior="INCLUDE",
        expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )

    assert updated["existing"] == {"value": 1}
    stored = updated[GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY]["gmail"]
    assert stored["watch_history_id"] == "123"
    assert stored["topic_name"] == "projects/proj/topics/gmail"

    parsed = get_subscription_metadata(updated, GoogleSubscriptionKind.GMAIL)
    assert parsed is not None
    assert parsed.kind == GoogleSubscriptionKind.GMAIL
    assert parsed.expires_at == datetime(2026, 6, 6, 12, 0, tzinfo=UTC)


def test_set_and_get_calendar_subscription_metadata_preserves_channel_fields() -> None:
    """Calendar channel metadata must round-trip the verification fields."""
    updated = set_calendar_subscription_metadata(
        {},
        channel_id="channel-1",
        resource_id="resource-1",
        resource_uri="https://www.googleapis.com/calendar/v3/calendars/primary/events",
        channel_token="secret-token",
        address="https://api.example.test/users/integrations/google/webhook?kind=calendar",
        expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )

    parsed = get_subscription_metadata(updated, GoogleSubscriptionKind.CALENDAR)
    assert parsed is not None
    assert parsed.kind == GoogleSubscriptionKind.CALENDAR
    assert parsed.channel_id == "channel-1"
    assert parsed.resource_id == "resource-1"
    assert parsed.channel_token == "secret-token"
