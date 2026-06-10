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
from src.service.integrations.webhooks.microsoft import ingestion_service
from src.service.integrations.webhooks.microsoft.ingestion_service import (
    MicrosoftWebhookIngestionService,
)
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    set_subscription_metadata,
)


def _integration_with_subscription(
    *,
    kind: MicrosoftSubscriptionKind,
    subscription_id: str,
    client_state: str,
) -> SimpleNamespace:
    metadata = set_subscription_metadata(
        {},
        kind=kind,
        subscription_id=subscription_id,
        client_state=client_state,
        resource="/me/events" if kind == MicrosoftSubscriptionKind.CALENDAR else "/me/messages",
        change_type="created,updated,deleted",
        expires_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )
    return SimpleNamespace(connection_metadata=metadata)


def _microsoft_parent_integration(
    *,
    organisation_id: UUID,
    integration_id: UUID,
) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.MICROSOFT,
        name="Microsoft",
        integration_scope=IntegrationScope.USER,
        integration_email="seller@example.com",
        connection_id="microsoft-connection",
        credentials={},
        scopes=[],
        is_active=True,
        connection_metadata={},
    )


def _outlook_email_sync_state(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    subscription_id: str = "sub-email",
    client_state: str = "state",
) -> IntegrationSyncState:
    return IntegrationSyncState(
        organisation_id=organisation_id,
        integration_id=integration_id,
        capability=IntegrationType.OUTLOOK_EMAIL,
        enabled=True,
        webhook_metadata=set_subscription_metadata(
            {},
            kind=MicrosoftSubscriptionKind.EMAIL,
            subscription_id=subscription_id,
            client_state=client_state,
            resource="/me/messages",
            change_type="created,updated,deleted",
            expires_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
            notification_url="https://api.example.test/users/integrations/microsoft/webhook",
            lifecycle_notification_url=(
                "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
            ),
        ),
    )


def _outlook_calendar_sync_state(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    subscription_id: str = "sub-calendar",
    client_state: str = "state",
) -> IntegrationSyncState:
    return IntegrationSyncState(
        organisation_id=organisation_id,
        integration_id=integration_id,
        capability=IntegrationType.OUTLOOK_CALENDAR,
        enabled=True,
        webhook_metadata=set_subscription_metadata(
            {},
            kind=MicrosoftSubscriptionKind.CALENDAR,
            subscription_id=subscription_id,
            client_state=client_state,
            resource="/me/events",
            change_type="created,updated,deleted",
            expires_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
            notification_url="https://api.example.test/users/integrations/microsoft/webhook",
            lifecycle_notification_url=(
                "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
            ),
        ),
    )


@pytest.mark.asyncio
async def test_ingest_calendar_notification_enqueues_calendar_sync(monkeypatch) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(
            return_value=_integration_with_subscription(
                kind=MicrosoftSubscriptionKind.CALENDAR,
                subscription_id="sub-calendar",
                client_state="state",
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.CALENDAR,
        notification_payloads=[
            {
                "subscriptionId": "sub-calendar",
                "clientState": "state",
                "changeType": "updated",
                "resource": "/me/events/event-1",
            }
        ],
    )

    assert result.queued_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_calendar_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "trigger_source": "webhook",
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=15,
    )


@pytest.mark.asyncio
async def test_ingest_calendar_notification_resolves_provider_parent_capability_state(
    monkeypatch,
) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    parent = _microsoft_parent_integration(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    state = _outlook_calendar_sync_state(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        ingestion_service,
        "acquire_org_async_session",
        lambda org_id: _fake_async_session(
            _FakeMicrosoftOrgSession(
                organisation_id=org_id,
                parent=parent,
                state=state,
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.CALENDAR,
        notification_payloads=[
            {
                "subscriptionId": "sub-calendar",
                "clientState": "state",
                "changeType": "updated",
                "resource": "/me/events/event-1",
            }
        ],
    )

    assert result.valid_count == 1
    assert result.queued_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_calendar_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "trigger_source": "webhook",
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=15,
    )


@pytest.mark.asyncio
async def test_ingest_multiple_valid_notifications_enqueue_one_sync(monkeypatch) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(
            return_value=_integration_with_subscription(
                kind=MicrosoftSubscriptionKind.EMAIL,
                subscription_id="sub-email",
                client_state="state",
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "state",
                "changeType": "created",
                "resource": "/me/messages/message-1",
            },
            {
                "subscriptionId": "sub-email",
                "clientState": "state",
                "changeType": "updated",
                "resource": "/me/messages/message-2",
            },
        ],
    )

    assert result.valid_count == 2
    assert result.queued_count == 1
    assert result.pending_count == 1
    send_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_email_notification_resolves_provider_parent_capability_state(
    monkeypatch,
) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    parent = _microsoft_parent_integration(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    state = _outlook_email_sync_state(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        ingestion_service,
        "acquire_org_async_session",
        lambda org_id: _fake_async_session(
            _FakeMicrosoftOrgSession(
                organisation_id=org_id,
                parent=parent,
                state=state,
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "state",
                "changeType": "created",
                "resource": "/me/messages/message-1",
            }
        ],
    )

    assert result.valid_count == 1
    assert result.queued_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_email_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "trigger_source": "webhook",
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=15,
    )


@pytest.mark.asyncio
async def test_ingest_duplicate_notification_does_not_enqueue(monkeypatch) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(
            return_value=_integration_with_subscription(
                kind=MicrosoftSubscriptionKind.EMAIL,
                subscription_id="sub-email",
                client_state="state",
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: False)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "state",
                "changeType": "created",
                "resource": "/me/messages/message-1",
            }
        ],
    )

    assert result.valid_count == 1
    assert result.queued_count == 0
    assert result.duplicate_count == 1
    assert result.pending_count == 0
    send_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_pending_notification_does_not_enqueue(monkeypatch) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(
            return_value=_integration_with_subscription(
                kind=MicrosoftSubscriptionKind.EMAIL,
                subscription_id="sub-email",
                client_state="state",
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: False)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "state",
                "changeType": "created",
                "resource": "/me/messages/message-1",
            }
        ],
    )

    assert result.valid_count == 1
    assert result.queued_count == 0
    assert result.duplicate_count == 0
    assert result.pending_count == 1
    send_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_rejects_client_state_mismatch(monkeypatch) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(
            return_value=_integration_with_subscription(
                kind=MicrosoftSubscriptionKind.EMAIL,
                subscription_id="sub-email",
                client_state="expected-state",
            )
        ),
    )
    monkeypatch.setattr(service, "_claim_dedupe", lambda *_: True)
    monkeypatch.setattr(service, "_claim_sync_pending", lambda *_: True)
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "wrong-state",
                "changeType": "created",
                "resource": "/me/messages/message-1",
            }
        ],
    )

    assert result.queued_count == 0
    assert result.invalid_count == 1
    send_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_lifecycle_matching_notification_enqueues_immediate_sync(monkeypatch) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(
            return_value=_integration_with_subscription(
                kind=MicrosoftSubscriptionKind.EMAIL,
                subscription_id="sub-email",
                client_state="expected-state",
            )
        ),
    )
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_lifecycle(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "expected-state",
                "changeType": "missed",
                "resource": "/me/messages",
            }
        ],
    )

    assert result.valid_count == 1
    assert result.queued_count == 1
    assert result.invalid_count == 0
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_email_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "trigger_source": "webhook",
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=None,
    )


@pytest.mark.asyncio
async def test_ingest_calendar_lifecycle_resolves_provider_parent_capability_state(
    monkeypatch,
) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    parent = _microsoft_parent_integration(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    state = _outlook_calendar_sync_state(
        organisation_id=organisation_id,
        integration_id=integration_id,
        client_state="expected-state",
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        ingestion_service,
        "acquire_org_async_session",
        lambda org_id: _fake_async_session(
            _FakeMicrosoftOrgSession(
                organisation_id=org_id,
                parent=parent,
                state=state,
            )
        ),
    )
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_lifecycle(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.CALENDAR,
        notification_payloads=[
            {
                "subscriptionId": "sub-calendar",
                "clientState": "expected-state",
                "changeType": "missed",
                "resource": "/me/events",
            }
        ],
    )

    assert result.valid_count == 1
    assert result.queued_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_calendar_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "trigger_source": "webhook",
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=None,
    )


@pytest.mark.asyncio
async def test_ingest_email_lifecycle_resolves_provider_parent_capability_state(
    monkeypatch,
) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    parent = _microsoft_parent_integration(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    state = _outlook_email_sync_state(
        organisation_id=organisation_id,
        integration_id=integration_id,
        client_state="expected-state",
    )
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        ingestion_service,
        "acquire_org_async_session",
        lambda org_id: _fake_async_session(
            _FakeMicrosoftOrgSession(
                organisation_id=org_id,
                parent=parent,
                state=state,
            )
        ),
    )
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_lifecycle(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "expected-state",
                "changeType": "missed",
                "resource": "/me/messages",
            }
        ],
    )

    assert result.valid_count == 1
    assert result.queued_count == 1
    send_task.assert_awaited_once_with(
        task_name="integrations.sync_email_integration",
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "trigger_source": "webhook",
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=None,
    )


@pytest.mark.asyncio
async def test_ingest_lifecycle_wrong_client_state_does_not_enqueue(monkeypatch) -> None:
    service = MicrosoftWebhookIngestionService()
    organisation_id = uuid4()
    integration_id = uuid4()
    send_task = AsyncMock(return_value="task-123")

    monkeypatch.setattr(
        service,
        "_load_integration",
        AsyncMock(
            return_value=_integration_with_subscription(
                kind=MicrosoftSubscriptionKind.EMAIL,
                subscription_id="sub-email",
                client_state="expected-state",
            )
        ),
    )
    monkeypatch.setattr(ingestion_service, "send_integrations_task_async", send_task)

    result = await service.ingest_lifecycle(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "wrong-state",
                "changeType": "missed",
                "resource": "/me/messages",
            }
        ],
    )

    assert result.valid_count == 0
    assert result.queued_count == 0
    assert result.invalid_count == 1
    send_task.assert_not_awaited()


async def _fake_async_session(session: object):
    yield session


class _FakeScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def first(self) -> object | None:
        return self._values[0] if self._values else None


class _FakeResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._values)

    def scalar_one_or_none(self) -> object | None:
        return self._values[0] if self._values else None


class _FakeMicrosoftOrgSession:
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
        if params.get("integration_id_1") != self.parent.integration_id:
            return _FakeResult([])
        if params.get("integration_type_1") == IntegrationType.MICROSOFT.value:
            return _FakeResult([self.parent])
        if params.get("capability_1") == IntegrationType.OUTLOOK_EMAIL:
            return _FakeResult([self.state])
        if params.get("capability_1") == IntegrationType.OUTLOOK_CALENDAR:
            return _FakeResult([self.state])
        return _FakeResult([])
