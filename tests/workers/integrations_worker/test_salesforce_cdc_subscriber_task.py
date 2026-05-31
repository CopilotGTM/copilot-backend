from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    SalesforcePubSubReplayIdError,
)
from src.workers.integrations_worker.tasks.crm.salesforce import salesforce_cdc_subscriber_task


def _run(coro):
    return asyncio.run(coro)


async def _acquire_session(_organisation_id):
    yield AsyncMock()


def test_completed_salesforce_cdc_run_releases_lease_before_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    released: list[dict[str, object]] = []
    queued: list[dict[str, object]] = []

    _patch_context(monkeypatch, org_id, integration_id)

    class FakePubSubClient:
        def __init__(self, **_kwargs):
            pass

        async def subscribe(self, **_kwargs):
            if False:
                yield None

    class FakeStateService(_BaseStateService):
        async def release_lease(self, **kwargs):
            released.append(kwargs)
            return True

    async def fake_send_integrations_task_async(**kwargs):
        queued.append(kwargs)
        return "task-id"

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforcePubSubClient", FakePubSubClient)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceCdcSubscriptionStateService",
        FakeStateService,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "send_integrations_task_async",
        fake_send_integrations_task_async,
    )

    result = _run(
        salesforce_cdc_subscriber_task._run_subscriber(
            organisation_id=org_id,
            integration_id=integration_id,
            channel="/data/OpportunityChangeEvent",
        )
    )

    assert result["status"] == "completed"
    assert len(released) == 1
    assert released[0]["organisation_id"] == org_id
    assert queued[0]["task_name"] == "integrations.run_salesforce_cdc_subscriber"


def test_salesforce_cdc_token_failure_releases_lease_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    released: list[dict[str, object]] = []
    queued: list[dict[str, object]] = []

    _patch_context(monkeypatch, org_id, integration_id)

    class FakeStateService(_BaseStateService):
        async def release_lease(self, **kwargs):
            released.append(kwargs)
            return True

    async def fake_get_access_token(_token_info):
        raise RuntimeError("token refresh failed")

    async def fake_send_integrations_task_async(**kwargs):
        queued.append(kwargs)
        return "task-id"

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "get_access_token", fake_get_access_token)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceCdcSubscriptionStateService",
        FakeStateService,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "send_integrations_task_async",
        fake_send_integrations_task_async,
    )

    with pytest.raises(RuntimeError, match="token refresh failed"):
        _run(
            salesforce_cdc_subscriber_task._run_subscriber(
                organisation_id=org_id,
                integration_id=integration_id,
                channel="/data/OpportunityChangeEvent",
            )
        )

    assert len(released) == 1
    assert released[0]["integration_id"] == integration_id
    assert queued[0]["countdown"] == 60


def test_salesforce_cdc_replay_repair_releases_lease_before_repair_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    integration_id = uuid4()
    operations: list[str] = []

    _patch_context(monkeypatch, org_id, integration_id)

    class FakePubSubClient:
        def __init__(self, **_kwargs):
            pass

        async def subscribe(self, **_kwargs):
            raise SalesforcePubSubReplayIdError("replay ID outside retention")
            if False:
                yield None

    class FakeStateService(_BaseStateService):
        async def acquire_lease(self, **_kwargs):
            return SimpleNamespace(
                acquired=True, subscription=SimpleNamespace(last_replay_id="AAE=")
            )

        async def clear_replay_for_repair(self, **_kwargs):
            operations.append("clear")

        async def release_lease(self, **_kwargs):
            operations.append("release")
            return True

    async def fake_send_integrations_task_async(*_args, **_kwargs):
        operations.append("enqueue")
        return "task-id"

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "SalesforcePubSubClient", FakePubSubClient)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "SalesforceCdcSubscriptionStateService",
        FakeStateService,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "send_integrations_task_async",
        fake_send_integrations_task_async,
    )

    result = _run(
        salesforce_cdc_subscriber_task._run_subscriber(
            organisation_id=org_id,
            integration_id=integration_id,
            channel="/data/OpportunityChangeEvent",
        )
    )

    assert result["status"] == "replay_expired_repair_queued"
    assert operations == ["clear", "release", "enqueue"]


class _BaseStateService:
    async def acquire_lease(self, **_kwargs):
        return SimpleNamespace(acquired=True, subscription=SimpleNamespace(last_replay_id=None))

    async def mark_error(self, **_kwargs):
        return None

    async def release_lease(self, **_kwargs):
        return True


def _patch_context(
    monkeypatch: pytest.MonkeyPatch,
    org_id: UUID,
    integration_id: UUID,
) -> None:
    async def fake_load_integration(**_kwargs):
        return SimpleNamespace(
            organisation_id=org_id,
            integration_id=integration_id,
            credentials={"access_token": "token", "instance_url": "https://sf"},
            access_token_expires_at=None,
            integration_type="salesforce",
            scopes=[],
            connection_id="conn-1",
            connection_metadata={"salesforce_organization_id": "00Dxx0000000001"},
        )

    def fake_metadata(_integration):
        return SimpleNamespace(
            channel="/data/OpportunityChangeEvent",
            instance_url="https://sf",
            salesforce_organization_id="00Dxx0000000001",
            pubsub_endpoint="api.pubsub.salesforce.com:443",
        )

    monkeypatch.setattr(salesforce_cdc_subscriber_task, "_load_integration", fake_load_integration)
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task,
        "get_salesforce_opportunity_cdc_metadata",
        fake_metadata,
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task, "get_access_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        salesforce_cdc_subscriber_task, "acquire_org_async_session", _acquire_session
    )
