"""Regression tests for Google webhook ingestion decisions.

These tests lock the rules that keep webhook callbacks as safe triggers:
Gmail routes by account email, Calendar validates channel secrets, and both
paths only enqueue existing sync tasks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.webhooks.google import ingestion_service
from src.service.integrations.webhooks.google.ingestion_service import (
    GoogleWebhookIngestionService,
    GoogleWebhookIntegrationTarget,
)
from src.service.integrations.webhooks.google.metadata import (
    set_calendar_subscription_metadata,
    set_gmail_subscription_metadata,
)
from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)


def _gmail_integration(email: str = "seller@example.com") -> SimpleNamespace:
    metadata = set_gmail_subscription_metadata(
        {},
        watch_history_id="123",
        topic_name="projects/proj/topics/gmail",
        label_ids=["INBOX"],
        label_filter_behavior="INCLUDE",
        expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )
    return SimpleNamespace(connection_metadata=metadata, integration_email=email)


def _google_parent_integration(
    *,
    organisation_id: UUID,
    integration_id: UUID,
) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.GOOGLE,
        name="Google",
        integration_scope=IntegrationScope.USER,
        integration_email="seller@example.com",
        connection_id="google-connection",
        credentials={},
        scopes=[],
        is_active=True,
        connection_metadata={},
    )


def _gmail_sync_state(
    *,
    organisation_id: UUID,
    integration_id: UUID,
) -> IntegrationSyncState:
    return IntegrationSyncState(
        organisation_id=organisation_id,
        integration_id=integration_id,
        capability=IntegrationType.GMAIL,
        enabled=True,
        webhook_metadata=set_gmail_subscription_metadata(
            {},
            watch_history_id="123",
            topic_name="projects/proj/topics/gmail",
            label_ids=["INBOX"],
            label_filter_behavior="INCLUDE",
            expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
        ),
    )


def _google_calendar_sync_state(
    *,
    organisation_id: UUID,
    integration_id: UUID,
) -> IntegrationSyncState:
    return IntegrationSyncState(
        organisation_id=organisation_id,
        integration_id=integration_id,
        capability=IntegrationType.GOOGLE_CALENDAR,
        enabled=True,
        webhook_metadata=set_calendar_subscription_metadata(
            {},
            channel_id="channel-1",
            resource_id="resource-1",
            resource_uri="https://www.googleapis.com/calendar/v3/calendars/primary/events",
            channel_token="secret",
            address="https://api.example.test/users/integrations/google/webhook?kind=calendar",
            expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
        ),
    )


def _calendar_integration() -> SimpleNamespace:
    metadata = set_calendar_subscription_metadata(
        {},
        channel_id="channel-1",
        resource_id="resource-1",
        resource_uri="https://www.googleapis.com/calendar/v3/calendars/primary/events",
        channel_token="secret",
        address="https://api.example.test/users/integrations/google/webhook?kind=calendar",
        expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )
    return SimpleNamespace(connection_metadata=metadata)


def _gmail_notification(message_id: str = "pubsub-1") -> GoogleGmailPubSubNotification:
    return GoogleGmailPubSubNotification(
        message_id=message_id,
        email_address="seller@example.com",
        history_id="987",
        publish_time=None,
        subscription=None,
        raw={},
    )


def _calendar_notification(
    *,
    channel_token: str = "secret",
    resource_state: str = "exists",
    message_number: str = "2",
) -> GoogleCalendarChannelNotification:
    return GoogleCalendarChannelNotification(
        channel_id="channel-1",
        resource_id="resource-1",
        resource_state=resource_state,
        message_number=message_number,
        resource_uri=None,
        channel_token=channel_token,
        channel_expiration=None,
    )


@pytest.mark.asyncio
async def test_gmail_ingestion_rejects_wrong_integration_type(monkeypatch) -> None:
    """Unknown Gmail accounts must acknowledge without enqueueing sync work."""
    service = GoogleWebhookIngestionService()
    monkeypatch.setattr(service, "_load_gmail_targets_by_email", AsyncMock(return_value=[]))

    result = await service.ingest_gmail(notification=_gmail_notification())

    assert result.invalid_count == 1
    assert result.queued_count == 0


@pytest.mark.asyncio
async def test_gmail_ingestion_rejects_mismatched_account_email(monkeypatch) -> None:
    """A Pub/Sub email mismatch must not wake an unrelated integration."""
    service = GoogleWebhookIngestionService()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_gmail_targets_by_email",
        AsyncMock(
            return_value=[
                GoogleWebhookIntegrationTarget(
                    organisation_id=uuid4(),
                    integration_id=uuid4(),
                    integration=_gmail_integration(email="other@example.com"),
                )
            ]
        ),
    )
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_gmail(notification=_gmail_notification())

    assert result.invalid_count == 1
    assert result.queued_count == 0
    send_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_gmail_ingestion_dedupes_pubsub_message_id_and_enqueues_email_sync(
    monkeypatch,
) -> None:
    """Replayed Gmail Pub/Sub messages must enqueue at most one email sync."""
    service = GoogleWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")
    seen_tokens: set[str] = set()

    monkeypatch.setattr(
        service,
        "_load_gmail_targets_by_email",
        AsyncMock(
            return_value=[
                GoogleWebhookIntegrationTarget(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    integration=_gmail_integration(),
                )
            ]
        ),
    )
    monkeypatch.setattr(
        service,
        "_claim_dedupe",
        lambda token: False if token in seen_tokens else not seen_tokens.add(token),
    )
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    first = await service.ingest_gmail(notification=_gmail_notification())
    second = await service.ingest_gmail(notification=_gmail_notification())

    assert first.queued_count == 1
    assert second.duplicate_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_email_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=15,
    )


@pytest.mark.asyncio
async def test_gmail_ingestion_fans_out_to_matching_account_integrations(monkeypatch) -> None:
    """A shared Gmail Pub/Sub endpoint can wake every active matching integration."""
    service = GoogleWebhookIngestionService()
    target_one = GoogleWebhookIntegrationTarget(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        integration=_gmail_integration(),
    )
    target_two = GoogleWebhookIntegrationTarget(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        integration=_gmail_integration(),
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_gmail_targets_by_email",
        AsyncMock(return_value=[target_one, target_two]),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_gmail(notification=_gmail_notification())

    assert result.valid_count == 2
    assert result.queued_count == 2
    assert send_task.await_count == 2


@pytest.mark.asyncio
async def test_gmail_ingestion_resolves_provider_parent_capability_state(monkeypatch) -> None:
    """Gmail Pub/Sub should wake the Google parent row when Gmail capability is enabled."""
    service = GoogleWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    parent = _google_parent_integration(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    state = _gmail_sync_state(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        ingestion_service,
        "acquire_master_async_session",
        lambda: _fake_async_session(_FakeMasterSession([organisation_id])),
    )
    monkeypatch.setattr(
        ingestion_service,
        "acquire_org_async_session",
        lambda org_id: _fake_async_session(
            _FakeGoogleOrgSession(
                organisation_id=org_id,
                parent=parent,
                state=state,
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_gmail(notification=_gmail_notification())

    assert result.valid_count == 1
    assert result.queued_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_email_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=15,
    )


@pytest.mark.asyncio
async def test_calendar_ingestion_verifies_channel_id_resource_id_and_token(monkeypatch) -> None:
    """Calendar callbacks must match stored channel metadata before enqueueing sync."""
    service = GoogleWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(return_value=_calendar_integration()),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    valid = await service.ingest_calendar(
        organisation_id=organisation_id,
        integration_id=integration_id,
        notification=_calendar_notification(),
    )
    invalid = await service.ingest_calendar(
        organisation_id=organisation_id,
        integration_id=integration_id,
        notification=_calendar_notification(channel_token="wrong"),
    )

    assert valid.queued_count == 1
    assert invalid.invalid_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_calendar_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=15,
    )


@pytest.mark.asyncio
async def test_calendar_ingestion_resolves_provider_parent_capability_state(
    monkeypatch,
) -> None:
    """Calendar webhooks should validate against Google parent capability metadata."""
    service = GoogleWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    parent = _google_parent_integration(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    state = _google_calendar_sync_state(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        ingestion_service,
        "acquire_org_async_session",
        lambda org_id: _fake_async_session(
            _FakeGoogleOrgSession(
                organisation_id=org_id,
                parent=parent,
                state=state,
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_calendar(
        organisation_id=organisation_id,
        integration_id=integration_id,
        notification=_calendar_notification(),
    )

    assert result.valid_count == 1
    assert result.queued_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_calendar_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=15,
    )


@pytest.mark.asyncio
async def test_calendar_sync_notification_resolves_provider_parent_without_enqueue(
    monkeypatch,
) -> None:
    """Calendar's initial sync callback should use capability metadata but not sync."""
    service = GoogleWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    parent = _google_parent_integration(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    state = _google_calendar_sync_state(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        ingestion_service,
        "acquire_org_async_session",
        lambda org_id: _fake_async_session(
            _FakeGoogleOrgSession(
                organisation_id=org_id,
                parent=parent,
                state=state,
            )
        ),
    )
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_calendar(
        organisation_id=organisation_id,
        integration_id=integration_id,
        notification=_calendar_notification(resource_state="sync"),
    )

    assert result.valid_count == 1
    assert result.queued_count == 0
    send_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_calendar_sync_notification_is_acknowledged_without_sync_enqueue(
    monkeypatch,
) -> None:
    """Calendar's initial sync-state callback is an acknowledgement, not a data change."""
    service = GoogleWebhookIngestionService()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(return_value=_calendar_integration()),
    )
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_calendar(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        notification=_calendar_notification(resource_state="sync"),
    )

    assert result.valid_count == 1
    assert result.queued_count == 0
    send_task.assert_not_awaited()


async def _fake_async_session(session: object):
    yield session


class _FakeScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values

    def first(self) -> object | None:
        return self._values[0] if self._values else None


class _FakeResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._values)

    def scalar_one_or_none(self) -> object | None:
        return self._values[0] if self._values else None


class _FakeMasterSession:
    def __init__(self, organisation_ids: list[UUID]) -> None:
        self._organisation_ids = organisation_ids

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self._organisation_ids)


class _FakeGoogleOrgSession:
    def __init__(
        self,
        *,
        organisation_id: UUID,
        parent: KnowledgeIntegration,
        state: IntegrationSyncState,
    ) -> None:
        self.organisation_id = organisation_id
        self.parent = parent
        self.state = state

    async def execute(self, statement: object) -> _FakeResult:
        params = statement.compile().params
        if params.get("integration_type_1") == IntegrationType.GOOGLE.value:
            return _FakeResult([self.parent])
        if params.get("capability_1") == IntegrationType.GMAIL:
            return _FakeResult([self.state])
        if params.get("capability_1") == IntegrationType.GOOGLE_CALENDAR:
            return _FakeResult([self.state])
        return _FakeResult([])
