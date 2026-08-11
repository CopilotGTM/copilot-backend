from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.salesforce.subscription_state import (
    SalesforceCdcSubscriptionStateService,
)


class _ScalarResult:
    def __init__(self, integration: KnowledgeIntegration | None) -> None:
        self._integration = integration

    def scalar_one_or_none(self) -> KnowledgeIntegration | None:
        return self._integration


class _FakeSession:
    def __init__(self, integration: KnowledgeIntegration | None) -> None:
        self.integration = integration
        self.commits = 0
        self.added: list[KnowledgeIntegration] = []

    async def execute(self, _stmt: Any) -> _ScalarResult:
        return _ScalarResult(self.integration)

    def add(self, integration: KnowledgeIntegration) -> None:
        self.added.append(integration)

    async def commit(self) -> None:
        self.commits += 1


def _integration(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    metadata: dict[str, Any] | None = None,
) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={},
        connection_metadata=metadata
        or {
            "salesforce_cdc": {
                "opportunity": {
                    "enabled": True,
                    "channel": "/data/OpportunityChangeEvent",
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_acquire_lease_blocks_duplicate_owner() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    first = await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/OpportunityChangeEvent",
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )
    second = await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/OpportunityChangeEvent",
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-b",
        lease_seconds=120,
    )

    assert first.acquired is True
    assert second.acquired is False


@pytest.mark.asyncio
async def test_subscription_state_keeps_replay_cursors_per_channel() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    session = _FakeSession(
        _integration(
            organisation_id=org_id,
            integration_id=integration_id,
            metadata={
                "salesforce_cdc": {
                    "opportunity": {"enabled": True, "channel": "/data/OpportunityChangeEvent"},
                    "account": {"enabled": True, "channel": "/data/AccountChangeEvent"},
                    "contact": {"enabled": True, "channel": "/data/ContactChangeEvent"},
                }
            },
        )
    )

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/OpportunityChangeEvent",
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )
    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/AccountChangeEvent",
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-b",
        lease_seconds=120,
    )
    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/ContactChangeEvent",
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-c",
        lease_seconds=120,
    )
    await service.store_replay_after_commit(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/OpportunityChangeEvent",
        replay_id="opportunity-replay",
        lease_owner="worker-a",
    )
    await service.store_replay_after_commit(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/AccountChangeEvent",
        replay_id="account-replay",
        lease_owner="worker-b",
    )
    await service.store_replay_after_commit(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/ContactChangeEvent",
        replay_id="contact-replay",
        lease_owner="worker-c",
    )

    opportunity = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/OpportunityChangeEvent",
    )
    account = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/AccountChangeEvent",
    )
    contact = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel="/data/ContactChangeEvent",
    )

    assert opportunity is not None
    assert account is not None
    assert contact is not None
    assert opportunity.last_replay_id == "opportunity-replay"
    assert account.last_replay_id == "account-replay"
    assert contact.last_replay_id == "contact-replay"


@pytest.mark.asyncio
async def test_replay_write_requires_current_lease_owner() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/OpportunityChangeEvent"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )

    stored = await service.store_replay_after_commit(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        replay_id="AAE=",
        lease_owner="worker-b",
    )

    state = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )
    assert stored is False
    assert state is not None
    assert state.last_replay_id is None


@pytest.mark.asyncio
async def test_keepalive_write_requires_current_lease_owner() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/OpportunityChangeEvent"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )

    stored = await service.store_keepalive_replay(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        replay_id="AAU=",
        lease_owner="worker-b",
    )

    state = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )
    assert stored is False
    assert state is not None
    assert state.last_replay_id is None


@pytest.mark.asyncio
async def test_keepalive_updates_latest_replay_cursor() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/OpportunityChangeEvent"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )
    await service.store_replay_after_commit(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        replay_id="AAE=",
        lease_owner="worker-a",
    )
    stored = await service.store_keepalive_replay(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        replay_id="AAU=",
        lease_owner="worker-a",
    )

    state = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )
    assert stored is True
    assert state is not None
    assert state.last_replay_id == "AAU="


@pytest.mark.asyncio
async def test_clear_replay_for_repair_removes_processed_and_keepalive_cursors() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/OpportunityChangeEvent"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )
    await service.store_replay_after_commit(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        replay_id="AAE=",
        lease_owner="worker-a",
    )
    await service.store_keepalive_replay(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        replay_id="AAU=",
        lease_owner="worker-a",
    )

    await service.clear_replay_for_repair(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )

    state = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )
    assert state is not None
    assert state.last_replay_id is None


@pytest.mark.asyncio
async def test_release_lease_allows_next_subscriber_owner_to_acquire() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/OpportunityChangeEvent"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )

    released = await service.release_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        lease_owner="worker-a",
    )
    second = await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-b",
        lease_seconds=120,
    )

    assert released is True
    assert second.acquired is True


@pytest.mark.asyncio
async def test_release_lease_rejects_non_owner() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/OpportunityChangeEvent"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )

    released = await service.release_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        lease_owner="worker-b",
    )
    second = await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-c",
        lease_seconds=120,
    )

    assert released is False
    assert second.acquired is False


@pytest.mark.asyncio
async def test_aggregate_owner_can_heartbeat_and_non_owner_cannot_advance_state() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/ChangeEvents"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="worker-a",
        lease_seconds=120,
    )

    heartbeat = await service.mark_heartbeat(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        lease_owner="worker-a",
    )
    renewed = await service.renew_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        lease_owner="worker-b",
        lease_seconds=120,
    )
    stored = await service.store_replay_after_commit(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
        replay_id="AAE=",
        lease_owner="worker-b",
    )
    state = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )

    assert heartbeat is True
    assert renewed is False
    assert stored is False
    assert state is not None
    assert state.heartbeat_at is not None
    assert state.last_replay_id is None


@pytest.mark.asyncio
async def test_aggregate_repair_is_single_flight_and_success_restarts_from_earliest() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    channel = "/data/ChangeEvents"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    first = await service.mark_repairing(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )
    second = await service.mark_repairing(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )
    succeeded = await service.mark_repair_succeeded(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )
    state = await service.get_subscription_state(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=channel,
    )

    assert first is True
    assert second is False
    assert succeeded is True
    assert state is not None
    assert state.status == "pending"
    assert state.last_replay_id is None
    assert state.replay_preset == "EARLIEST"
    assert state.repair_attempts == 0


@pytest.mark.asyncio
async def test_no_unexpired_per_object_lease_allows_aggregate_cutover() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    session = _FakeSession(
        _integration(
            organisation_id=org_id,
            integration_id=integration_id,
            metadata={
                "salesforce_cdc": {
                    "opportunity": {"lease_owner": "legacy", "lease_expires_at": expired},
                    "account": {"lease_owner": None, "lease_expires_at": None},
                }
            },
        )
    )

    assert (
        await service.has_active_legacy_leases(
            session=session,
            organisation_id=org_id,
            integration_id=integration_id,
        )
        is False
    )


@pytest.mark.asyncio
async def test_aggregate_mode_fences_legacy_lease_renewal() -> None:
    service = SalesforceCdcSubscriptionStateService()
    org_id = uuid4()
    integration_id = uuid4()
    legacy_channel = "/data/OpportunityChangeEvent"
    session = _FakeSession(_integration(organisation_id=org_id, integration_id=integration_id))

    await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=legacy_channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="legacy-worker",
        lease_seconds=120,
    )
    metadata = dict(session.integration.connection_metadata or {})
    metadata["salesforce_cdc"] = dict(metadata["salesforce_cdc"])
    metadata["salesforce_cdc"]["mode"] = "aggregate"
    session.integration.connection_metadata = metadata

    renewed = await service.renew_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=legacy_channel,
        lease_owner="legacy-worker",
        lease_seconds=120,
    )
    reacquired = await service.acquire_lease(
        session=session,
        organisation_id=org_id,
        integration_id=integration_id,
        channel=legacy_channel,
        salesforce_organization_id="00Dxx0000000001",
        lease_owner="another-legacy-worker",
        lease_seconds=120,
    )

    assert renewed is False
    assert reacquired.acquired is False
