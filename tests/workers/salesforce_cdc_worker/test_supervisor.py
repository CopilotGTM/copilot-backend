import asyncio
from uuid import uuid4

import pytest
from src.workers.salesforce_cdc_worker.config import SalesforceCdcWorkerSettings
from src.workers.salesforce_cdc_worker.connector import SalesforceIntegrationRef
from src.workers.salesforce_cdc_worker.supervisor import SalesforceCdcSupervisor


def _integration() -> SalesforceIntegrationRef:
    return SalesforceIntegrationRef(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        instance_url="https://acme.my.salesforce.com",
        salesforce_organization_id="00Dxx0000000001",
        pubsub_endpoint="api.pubsub.salesforce.com:443",
        enabled_entities=frozenset({"Opportunity"}),
    )


@pytest.mark.asyncio
async def test_reconcile_runs_seven_and_marks_eighth_without_disrupting_slots() -> None:
    integrations = [_integration() for _ in range(8)]
    started: list[object] = []
    capacity: list[object] = []
    blocked = asyncio.Event()

    class FakeConnector:
        async def run(self, integration, _stop):
            started.append(integration.integration_id)
            await blocked.wait()

    supervisor = SalesforceCdcSupervisor(
        SalesforceCdcWorkerSettings(max_integrations=7),
        discover=lambda: integrations,
        connector_factory=FakeConnector,
        has_active_legacy_leases=lambda _integration: False,
        mark_capacity_exceeded=lambda integration: capacity.append(integration.integration_id),
    )

    await supervisor.reconcile()
    await asyncio.sleep(0)
    running_before = supervisor.running_keys
    await supervisor.reconcile()

    assert len(started) == 7
    assert len(running_before) == 7
    assert supervisor.running_keys == running_before
    assert capacity == [integrations[7].integration_id, integrations[7].integration_id]
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_active_legacy_lease_keeps_aggregate_connector_pending() -> None:
    integration = _integration()
    started: list[object] = []

    class FakeConnector:
        async def run(self, item, _stop):
            started.append(item.integration_id)

    supervisor = SalesforceCdcSupervisor(
        SalesforceCdcWorkerSettings(),
        discover=lambda: [integration],
        connector_factory=FakeConnector,
        has_active_legacy_leases=lambda _integration: True,
        mark_capacity_exceeded=lambda _integration: None,
    )

    await supervisor.reconcile()

    assert started == []
    assert supervisor.running_keys == frozenset()


@pytest.mark.asyncio
async def test_removing_integration_cancels_only_its_connector() -> None:
    integrations = [_integration(), _integration()]
    blocked = asyncio.Event()
    cancelled: list[object] = []

    class FakeConnector:
        async def run(self, integration, _stop):
            try:
                await blocked.wait()
            finally:
                cancelled.append(integration.integration_id)

    supervisor = SalesforceCdcSupervisor(
        SalesforceCdcWorkerSettings(),
        discover=lambda: integrations,
        connector_factory=FakeConnector,
        has_active_legacy_leases=lambda _integration: False,
        mark_capacity_exceeded=lambda _integration: None,
    )
    await supervisor.reconcile()
    await asyncio.sleep(0)
    retained = integrations[0]
    removed = integrations[1]
    integrations[:] = [retained]

    await supervisor.reconcile()

    assert supervisor.running_keys == {(retained.organisation_id, retained.integration_id)}
    assert cancelled == [removed.integration_id]
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_failed_connector_restarts_without_cancelling_peer() -> None:
    failing = _integration()
    peer = _integration()
    blocked = asyncio.Event()
    starts: dict[object, int] = {}

    class FakeConnector:
        async def run(self, integration, _stop):
            starts[integration.integration_id] = starts.get(integration.integration_id, 0) + 1
            if (
                integration.integration_id == failing.integration_id
                and starts[integration.integration_id] == 1
            ):
                raise RuntimeError("connector failed")
            await blocked.wait()

    supervisor = SalesforceCdcSupervisor(
        SalesforceCdcWorkerSettings(),
        discover=lambda: [failing, peer],
        connector_factory=FakeConnector,
        has_active_legacy_leases=lambda _integration: False,
        mark_capacity_exceeded=lambda _integration: None,
    )
    await supervisor.reconcile()
    await asyncio.sleep(0)
    await supervisor.reconcile()
    await asyncio.sleep(0)

    assert starts[failing.integration_id] == 2
    assert starts[peer.integration_id] == 1
    assert len(supervisor.running_keys) == 2
    await supervisor.shutdown()
