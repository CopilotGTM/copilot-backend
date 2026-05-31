"""Regression tests for Google webhook subscription API calls.

These tests lock the provider payloads and stored metadata shapes used by the
subscription service without making live Gmail or Calendar requests.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.google.metadata import (
    GoogleSubscriptionKind,
    get_subscription_metadata,
    set_gmail_subscription_metadata,
)
from src.service.integrations.webhooks.google.subscription_service import (
    GoogleSubscriptionService,
)


def _integration(
    *,
    integration_type: IntegrationType,
    metadata: dict | None = None,
) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=integration_type,
        name=integration_type.value,
        credentials={},
        connection_id="conn",
        is_active=True,
        connection_metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_gmail_subscription_uses_users_watch_and_stores_expiration(monkeypatch) -> None:
    """Gmail watch creation must store Google's history id and expiration."""
    captured_payloads: list[dict] = []

    async def fake_get_access_token(token_info):
        """Avoid live token refresh while exercising the provider payload."""
        return "access-token"

    async def fake_gmail_api_post_json(client, endpoint, *, json_body):
        """Capture Gmail watch payload and return the fields Google requires."""
        captured_payloads.append({"endpoint": endpoint, "json_body": json_body})
        return {"historyId": "123", "expiration": "1780747200000"}

    monkeypatch.setenv("GOOGLE_GMAIL_PUBSUB_TOPIC", "projects/proj/topics/gmail")
    monkeypatch.setattr(
        "src.service.integrations.webhooks.google.subscription_service.get_access_token",
        fake_get_access_token,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.google.subscription_service.gmail_api_post_json",
        fake_gmail_api_post_json,
    )

    integration = _integration(integration_type=IntegrationType.GMAIL)
    metadata = await GoogleSubscriptionService().ensure_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=GoogleSubscriptionKind.GMAIL,
    )

    assert captured_payloads == [
        {
            "endpoint": "/gmail/v1/users/me/watch",
            "json_body": {
                "topicName": "projects/proj/topics/gmail",
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "INCLUDE",
            },
        }
    ]
    stored = get_subscription_metadata(metadata, GoogleSubscriptionKind.GMAIL)
    assert stored is not None
    assert stored.watch_history_id == "123"
    assert stored.expires_at == datetime(2026, 6, 6, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_calendar_subscription_uses_events_watch_and_stores_resource_id(
    monkeypatch,
) -> None:
    """Calendar watch creation must store channel verification metadata."""
    captured_payloads: list[dict] = []

    async def fake_get_access_token(token_info):
        """Avoid live token refresh while exercising the provider payload."""
        return "access-token"

    async def fake_calendar_api_post_json(client, endpoint, *, json_body):
        """Capture Calendar watch payload and return channel identifiers."""
        captured_payloads.append({"endpoint": endpoint, "json_body": json_body})
        return {
            "resourceId": "resource-1",
            "resourceUri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "expiration": "1780747200000",
        }

    monkeypatch.setenv("GOOGLE_WEBHOOK_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(
        "src.service.integrations.webhooks.google.subscription_service.get_access_token",
        fake_get_access_token,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.google.subscription_service.calendar_api_post_json",
        fake_calendar_api_post_json,
    )

    integration = _integration(integration_type=IntegrationType.GOOGLE_CALENDAR)
    metadata = await GoogleSubscriptionService().ensure_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=GoogleSubscriptionKind.CALENDAR,
    )

    assert captured_payloads[0]["endpoint"] == "/calendar/v3/calendars/primary/events/watch"
    assert captured_payloads[0]["json_body"]["type"] == "web_hook"
    assert captured_payloads[0]["json_body"]["address"].startswith(
        "https://api.example.test/users/integrations/google/webhook?"
    )
    assert "kind=calendar" in captured_payloads[0]["json_body"]["address"]
    stored = get_subscription_metadata(metadata, GoogleSubscriptionKind.CALENDAR)
    assert stored is not None
    assert stored.resource_id == "resource-1"
    assert stored.channel_id == captured_payloads[0]["json_body"]["id"]
    assert stored.channel_token == captured_payloads[0]["json_body"]["token"]


@pytest.mark.asyncio
async def test_existing_unexpired_subscription_is_reused(monkeypatch) -> None:
    """Fresh stored metadata must prevent unnecessary provider watch renewal."""

    async def fail_gmail_api_post_json(client, endpoint, *, json_body):
        """Fail if the service calls Google despite a fresh existing watch."""
        raise AssertionError("Google should not be called for a fresh existing watch")

    existing_metadata = set_gmail_subscription_metadata(
        {},
        watch_history_id="123",
        topic_name="projects/proj/topics/gmail",
        label_ids=["INBOX"],
        label_filter_behavior="INCLUDE",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.google.subscription_service.gmail_api_post_json",
        fail_gmail_api_post_json,
    )
    integration = _integration(
        integration_type=IntegrationType.GMAIL,
        metadata=existing_metadata,
    )

    metadata = await GoogleSubscriptionService().ensure_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=GoogleSubscriptionKind.GMAIL,
    )

    stored = get_subscription_metadata(metadata, GoogleSubscriptionKind.GMAIL)
    assert stored is not None
    assert stored.watch_history_id == "123"


def test_calendar_payload_uses_public_base_url_and_routing_params(monkeypatch) -> None:
    """Calendar callback URLs must bind the channel to one org integration."""
    monkeypatch.setenv("GOOGLE_WEBHOOK_BASE_URL", "https://api.example.test/")

    url = GoogleSubscriptionService()._calendar_callback_url(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
    )

    assert url.startswith("https://api.example.test/users/integrations/google/webhook?")
    assert "kind=calendar" in url
    assert "organisation_id=11111111-1111-1111-1111-111111111111" in url
    assert "integration_id=22222222-2222-2222-2222-222222222222" in url
