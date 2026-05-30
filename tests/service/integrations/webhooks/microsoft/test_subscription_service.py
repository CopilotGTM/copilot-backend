from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.microsoft.metadata import (
    MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY,
    MicrosoftSubscriptionKind,
    get_subscription_metadata,
    set_subscription_metadata,
)
from src.service.integrations.webhooks.microsoft.subscription_service import (
    MicrosoftGraphSubscriptionService,
)
from src.service.integrations.webhooks.microsoft.types import MicrosoftGraphWebhookNotification


def test_set_and_get_subscription_metadata_round_trips_existing_metadata() -> None:
    metadata = {"existing": {"value": 1}}

    updated = set_subscription_metadata(
        metadata,
        kind=MicrosoftSubscriptionKind.CALENDAR,
        subscription_id="sub-calendar",
        client_state="secret",
        resource="/me/events",
        change_type="created,updated,deleted",
        expires_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )

    assert updated["existing"] == {"value": 1}
    stored = updated[MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY]["calendar"]
    assert stored["subscription_id"] == "sub-calendar"
    assert stored["client_state"] == "secret"
    assert stored["resource"] == "/me/events"
    assert stored["expires_at"] == "2026-06-03T12:00:00+00:00"

    parsed = get_subscription_metadata(updated, MicrosoftSubscriptionKind.CALENDAR)
    assert parsed is not None
    assert parsed.subscription_id == "sub-calendar"
    assert parsed.kind == MicrosoftSubscriptionKind.CALENDAR


def test_set_subscription_metadata_replaces_malformed_subscription_container() -> None:
    metadata = {MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY: "not-a-container"}

    updated = set_subscription_metadata(
        metadata,
        kind=MicrosoftSubscriptionKind.EMAIL,
        subscription_id="sub-email",
        client_state="secret",
        resource="/me/messages",
        change_type="created,updated",
        expires_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )

    assert updated[MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY]["email"]["subscription_id"] == "sub-email"


def test_webhook_notification_from_payload_rejects_non_dict_payloads() -> None:
    assert MicrosoftGraphWebhookNotification.from_payload(None) is None
    assert MicrosoftGraphWebhookNotification.from_payload([]) is None


def test_webhook_notification_dedupe_token_fallback_uses_raw_payload_entropy() -> None:
    base_payload = {
        "subscriptionId": "sub-calendar",
        "clientState": "secret",
        "changeType": "updated",
        "resource": "/me/events/event-1",
    }
    first = MicrosoftGraphWebhookNotification.from_payload({**base_payload, "sequenceNumber": 1})
    second = MicrosoftGraphWebhookNotification.from_payload({**base_payload, "sequenceNumber": 2})

    assert first is not None
    assert second is not None
    assert first.dedupe_token() != second.dedupe_token()


def test_webhook_notification_accepts_lifecycle_payloads() -> None:
    notification = MicrosoftGraphWebhookNotification.from_payload(
        {
            "subscriptionId": "sub-calendar",
            "clientState": "secret",
            "lifecycleEvent": "missed",
            "resource": "/me/events",
        }
    )

    assert notification is not None
    assert notification.change_type == "missed"


def _outlook_calendar_integration(
    *,
    metadata: dict | None = None,
) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.OUTLOOK_CALENDAR,
        name="Outlook Calendar",
        credentials={},
        connection_id="conn",
        is_active=True,
        connection_metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_ensure_subscription_creates_calendar_subscription(monkeypatch) -> None:
    captured_payloads: list[dict] = []

    async def fake_graph_post_json(client, endpoint, *, json_body, params=None):
        captured_payloads.append(json_body)
        return {
            "id": "sub-calendar",
            "expirationDateTime": "2026-06-03T12:00:00Z",
        }

    async def fake_get_access_token(token_info):
        return "access-token"

    monkeypatch.setenv("MICROSOFT_GRAPH_WEBHOOK_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.graph_api_post_json",
        fake_graph_post_json,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.get_access_token",
        fake_get_access_token,
    )

    integration = _outlook_calendar_integration()

    metadata = await MicrosoftGraphSubscriptionService().ensure_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=MicrosoftSubscriptionKind.CALENDAR,
    )

    assert captured_payloads[0]["resource"] == "/me/events"
    assert captured_payloads[0]["changeType"] == "created,updated,deleted"
    requested_expiration = datetime.fromisoformat(
        captured_payloads[0]["expirationDateTime"].replace("Z", "+00:00")
    )
    assert requested_expiration <= datetime.now(UTC) + timedelta(minutes=4230)
    assert captured_payloads[0]["notificationUrl"].startswith(
        "https://api.example.test/users/integrations/microsoft/webhook?"
    )
    assert metadata["microsoft_graph_subscriptions"]["calendar"]["subscription_id"] == (
        "sub-calendar"
    )


@pytest.mark.asyncio
async def test_ensure_subscription_reuses_valid_existing_metadata(monkeypatch) -> None:
    async def fail_graph_post_json(client, endpoint, *, json_body, params=None):
        raise AssertionError("Graph should not be called for a fresh existing subscription")

    existing_metadata = set_subscription_metadata(
        {},
        kind=MicrosoftSubscriptionKind.CALENDAR,
        subscription_id="sub-calendar",
        client_state="existing-secret",
        resource="/me/events",
        change_type="created,updated,deleted",
        expires_at=datetime.now(UTC) + timedelta(days=3),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )
    integration = _outlook_calendar_integration(metadata=existing_metadata)
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.graph_api_post_json",
        fail_graph_post_json,
    )

    metadata = await MicrosoftGraphSubscriptionService().ensure_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=MicrosoftSubscriptionKind.CALENDAR,
    )

    stored = metadata["microsoft_graph_subscriptions"]["calendar"]
    assert stored["subscription_id"] == "sub-calendar"
    assert stored["client_state"] == "existing-secret"


@pytest.mark.asyncio
async def test_ensure_subscription_renews_near_expiry_without_changing_client_state(
    monkeypatch,
) -> None:
    patched_payloads: list[dict] = []

    async def fake_get_access_token(token_info):
        return "access-token"

    async def fake_graph_patch_json(client, endpoint, *, json_body, params=None):
        patched_payloads.append({"endpoint": endpoint, "json_body": json_body})
        return {"expirationDateTime": "2026-06-03T12:00:00Z"}

    existing_metadata = set_subscription_metadata(
        {},
        kind=MicrosoftSubscriptionKind.CALENDAR,
        subscription_id="sub-calendar",
        client_state="existing-secret",
        resource="/me/events",
        change_type="created,updated,deleted",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )
    integration = _outlook_calendar_integration(metadata=existing_metadata)
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.get_access_token",
        fake_get_access_token,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.graph_api_patch_json",
        fake_graph_patch_json,
    )

    metadata = await MicrosoftGraphSubscriptionService().ensure_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=MicrosoftSubscriptionKind.CALENDAR,
    )

    assert patched_payloads[0]["endpoint"] == "/v1.0/subscriptions/sub-calendar"
    stored = metadata["microsoft_graph_subscriptions"]["calendar"]
    assert stored["subscription_id"] == "sub-calendar"
    assert stored["client_state"] == "existing-secret"
    assert stored["expires_at"] == "2026-06-03T12:00:00+00:00"


@pytest.mark.asyncio
async def test_renew_subscription_recreates_when_graph_subscription_is_gone(
    monkeypatch,
) -> None:
    posted_payloads: list[dict] = []

    async def fake_get_access_token(token_info):
        return "access-token"

    async def fake_graph_patch_json(client, endpoint, *, json_body, params=None):
        request = httpx.Request("PATCH", "https://graph.microsoft.com/v1.0/subscriptions/missing")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("missing", request=request, response=response)

    async def fake_graph_post_json(client, endpoint, *, json_body, params=None):
        posted_payloads.append(json_body)
        return {
            "id": "new-sub-calendar",
            "expirationDateTime": "2026-06-03T12:00:00Z",
        }

    existing_metadata = set_subscription_metadata(
        {},
        kind=MicrosoftSubscriptionKind.CALENDAR,
        subscription_id="missing",
        client_state="old-secret",
        resource="/me/events",
        change_type="created,updated,deleted",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )
    integration = _outlook_calendar_integration(metadata=existing_metadata)
    monkeypatch.setenv("MICROSOFT_GRAPH_WEBHOOK_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.get_access_token",
        fake_get_access_token,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.graph_api_patch_json",
        fake_graph_patch_json,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.graph_api_post_json",
        fake_graph_post_json,
    )

    metadata = await MicrosoftGraphSubscriptionService().renew_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=MicrosoftSubscriptionKind.CALENDAR,
    )

    assert posted_payloads[0]["resource"] == "/me/events"
    stored = metadata["microsoft_graph_subscriptions"]["calendar"]
    assert stored["subscription_id"] == "new-sub-calendar"
    assert stored["client_state"] != "old-secret"


def test_build_subscription_payload_requires_webhook_base_url(monkeypatch) -> None:
    monkeypatch.delenv("MICROSOFT_GRAPH_WEBHOOK_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="MICROSOFT_GRAPH_WEBHOOK_BASE_URL is required"):
        MicrosoftGraphSubscriptionService()._build_subscription_payload(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            integration_id=UUID("22222222-2222-2222-2222-222222222222"),
            kind=MicrosoftSubscriptionKind.CALENDAR,
            client_state="secret",
        )
