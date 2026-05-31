"""Regression tests for Google subscription task orchestration.

The task service is responsible for session boundaries: load a snapshot,
perform provider work outside the session, then persist in a fresh session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.google.metadata import (
    GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY,
    GoogleSubscriptionKind,
)
from src.service.integrations.webhooks.google.subscription_task_service import (
    GoogleSubscriptionTaskService,
    kind_for_integration_type,
)


def _integration(integration_type: IntegrationType = IntegrationType.GMAIL) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=integration_type,
        name=integration_type.value,
        credentials={},
        connection_id="conn",
        is_active=True,
        connection_metadata={},
    )


class FakeGoogleSubscriptionService:
    """Subscription-service double that records provider ensure calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ensure_subscription_metadata(
        self,
        *,
        organisation_id,
        integration,
        kind,
    ) -> dict:
        """Return metadata in the same shape the real service persists."""
        self.calls.append(
            {
                "organisation_id": organisation_id,
                "integration": integration,
                "kind": kind,
            }
        )
        return {"google_webhook_subscriptions": {kind.value: {"subscription": "stored"}}}


class FakeScalarResult:
    """Small result double that mimics SQLAlchemy scalar collections."""

    def __init__(self, values: list[UUID]) -> None:
        self._values = values

    def all(self) -> list[UUID]:
        """Return fake scalar values from the query result."""
        return self._values


class FakeMasterResult:
    """Master-session result double used by all-org fanout tests."""

    def __init__(self, values: list[UUID]) -> None:
        self._values = values

    def scalars(self) -> FakeScalarResult:
        """Expose the scalar result API used by the task service."""
        return FakeScalarResult(self._values)


class FakeMasterSession:
    """Master-session double that returns approved organisation ids."""

    def __init__(self, organisation_ids: list[UUID]) -> None:
        self._organisation_ids = organisation_ids

    async def execute(self, statement) -> FakeMasterResult:
        """Return fake organisations without touching the master database."""
        return FakeMasterResult(self._organisation_ids)


class FakeIntegrationResult:
    """Result double for queries that load one integration row."""

    def __init__(self, integration: KnowledgeIntegration | None) -> None:
        self._integration = integration

    def scalar_one_or_none(self) -> KnowledgeIntegration | None:
        """Return the fake integration row."""
        return self._integration


class FakeOrgSession:
    """Org-session double used to prove reload-and-persist metadata behavior."""

    def __init__(self, integration: KnowledgeIntegration) -> None:
        self.integration = integration
        self.committed = False

    async def execute(self, statement) -> FakeIntegrationResult:
        """Return the configured integration for any eligible integration query."""
        return FakeIntegrationResult(self.integration)

    def add(self, instance: KnowledgeIntegration) -> None:
        """The task service should persist the same row it reloaded."""
        assert instance is self.integration

    async def commit(self) -> None:
        """Record that the service committed the refreshed metadata."""
        self.committed = True


@pytest.mark.asyncio
async def test_ensure_subscription_loads_snapshot_and_persists_after_api_call(
    monkeypatch,
) -> None:
    """Ensure must persist metadata only after provider work returns."""
    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    integration_id = UUID("22222222-2222-2222-2222-222222222222")
    subscription_service = FakeGoogleSubscriptionService()
    task_service = GoogleSubscriptionTaskService(subscription_service=subscription_service)
    persisted: list[dict] = []

    monkeypatch.setattr(
        task_service,
        "_load_integration_snapshot",
        AsyncMock(return_value=_integration()),
    )

    async def fake_persist_metadata(**kwargs) -> bool:
        """Capture the metadata that would be written in a fresh org session."""
        persisted.append(kwargs)
        return True

    monkeypatch.setattr(task_service, "_persist_metadata", fake_persist_metadata)

    result = await task_service.ensure_subscription(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )

    assert result["status"] == "success"
    assert result["kind"] == "gmail"
    assert subscription_service.calls[0]["kind"] == GoogleSubscriptionKind.GMAIL
    assert persisted[0]["metadata"]["google_webhook_subscriptions"]["gmail"] == {
        "subscription": "stored"
    }


@pytest.mark.asyncio
async def test_ensure_subscription_merges_webhook_metadata_into_current_row(
    monkeypatch,
) -> None:
    """A provider-call race must not overwrite sync cursors written by initial sync."""
    from src.service.integrations.webhooks.google import subscription_task_service

    organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    integration_id = UUID("22222222-2222-2222-2222-222222222222")
    snapshot_row = _integration()
    snapshot_row.connection_metadata = {
        "gmail_history_id": "old-history",
        GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY: {
            "gmail": {"watch_history_id": "old-watch"},
        },
    }
    current_row = _integration()
    current_row.connection_metadata = {
        "gmail_history_id": "new-history",
        "calendar_delta_link": "sync-token",
        GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY: {
            "calendar": {"channel_id": "current-calendar-channel"},
        },
    }
    snapshot_session = FakeOrgSession(snapshot_row)
    current_session = FakeOrgSession(current_row)
    sessions = [snapshot_session, current_session]

    async def fake_session_generator(org_id):
        assert org_id == organisation_id
        yield sessions.pop(0)

    async def fake_ensure(self, *, organisation_id, integration, kind):
        assert integration.connection_metadata["gmail_history_id"] == "old-history"
        return {
            "gmail_history_id": "old-history",
            GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY: {
                "gmail": {"watch_history_id": "new-watch"},
            },
        }

    monkeypatch.setattr(
        subscription_task_service,
        "acquire_org_async_session",
        fake_session_generator,
    )
    monkeypatch.setattr(
        subscription_task_service.GoogleSubscriptionService,
        "ensure_subscription_metadata",
        fake_ensure,
    )

    result = await GoogleSubscriptionTaskService().ensure_subscription(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )

    assert result["status"] == "success"
    assert current_session.committed is True
    assert current_row.connection_metadata["gmail_history_id"] == "new-history"
    assert current_row.connection_metadata["calendar_delta_link"] == "sync-token"
    assert current_row.connection_metadata[GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY] == {
        "calendar": {"channel_id": "current-calendar-channel"},
        "gmail": {"watch_history_id": "new-watch"},
    }


@pytest.mark.asyncio
async def test_dispatch_all_subscription_ensures_fans_out_by_integration(monkeypatch) -> None:
    """Scheduled renewal must dispatch one ensure per eligible integration."""
    org_id = uuid4()
    first_integration_id = uuid4()
    second_integration_id = uuid4()
    dispatched: list[tuple[UUID, UUID]] = []
    task_service = GoogleSubscriptionTaskService()

    async def fake_acquire_master_async_session():
        """Yield one approved org through the same async-generator contract."""
        yield FakeMasterSession([org_id])

    async def fake_load_org_integration_ids(organisation_id: UUID) -> list[UUID]:
        """Return two eligible integrations for the approved org."""
        assert organisation_id == org_id
        return [first_integration_id, second_integration_id]

    monkeypatch.setattr(
        "src.service.integrations.webhooks.google.subscription_task_service.acquire_master_async_session",
        fake_acquire_master_async_session,
    )
    monkeypatch.setattr(
        task_service,
        "_load_org_integration_ids",
        fake_load_org_integration_ids,
    )

    result = await task_service.dispatch_all_subscription_ensures(
        dispatch_subscription_ensure=lambda organisation_id, integration_id: dispatched.append(
            (organisation_id, integration_id)
        )
    )

    assert result["status"] == "success"
    assert result["orgs_checked"] == 1
    assert result["integrations_dispatched"] == 2
    assert dispatched == [(org_id, first_integration_id), (org_id, second_integration_id)]


def test_kind_for_integration_type_maps_only_google_email_and_calendar() -> None:
    """Only Gmail and Google Calendar participate in Google webhook renewal."""
    assert kind_for_integration_type(IntegrationType.GMAIL) == GoogleSubscriptionKind.GMAIL
    assert (
        kind_for_integration_type(IntegrationType.GOOGLE_CALENDAR)
        == GoogleSubscriptionKind.CALENDAR
    )
    assert kind_for_integration_type(IntegrationType.OUTLOOK_EMAIL) is None
