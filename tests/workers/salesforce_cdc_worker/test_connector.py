import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    SalesforcePubSubEvent,
    SalesforcePubSubKeepalive,
    SalesforcePubSubReplayIdError,
)
from src.workers.salesforce_cdc_worker.connector import (
    SalesforceAggregateConnector,
    SalesforceIntegrationRef,
)


async def _session_factory(_organisation_id):
    yield object()


def _integration_ref() -> SalesforceIntegrationRef:
    return SalesforceIntegrationRef(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        instance_url="https://acme.my.salesforce.com",
        salesforce_organization_id="00Dxx0000000001",
        pubsub_endpoint="api.pubsub.salesforce.com:443",
        enabled_entities=frozenset({"Opportunity"}),
    )


class _StateService:
    def __init__(self, *, replay_id: str | None = None, replay_preset: str | None = None) -> None:
        self.subscription = SimpleNamespace(
            last_replay_id=replay_id,
            replay_preset=replay_preset,
        )
        self.calls: list[str] = []

    async def acquire_lease(self, **_kwargs):
        self.calls.append("acquire")
        return SimpleNamespace(acquired=True, subscription=self.subscription)

    async def renew_lease(self, **_kwargs):
        self.calls.append("renew")
        return True

    async def store_replay_after_commit(self, **_kwargs):
        self.calls.append("store_replay")
        return True

    async def store_keepalive_replay(self, **_kwargs):
        self.calls.append("store_keepalive")
        return True

    async def mark_connected(self, **_kwargs):
        self.calls.append("connected")
        return True

    async def mark_disconnected(self, **_kwargs):
        self.calls.append("disconnected")
        return True

    async def mark_heartbeat(self, **_kwargs):
        self.calls.append("heartbeat")
        return True

    async def mark_repairing(self, **_kwargs):
        self.calls.append("repairing")
        return True

    async def release_lease(self, **_kwargs):
        self.calls.append("release")
        return True


@pytest.mark.asyncio
async def test_connector_processes_all_records_before_advancing_aggregate_replay() -> None:
    operations: list[str] = []
    subscriptions: list[dict[str, object]] = []
    stop = asyncio.Event()

    class FakeStateService:
        async def acquire_lease(self, **_kwargs):
            return SimpleNamespace(
                acquired=True,
                subscription=SimpleNamespace(last_replay_id=None, replay_preset="EARLIEST"),
            )

        async def store_replay_after_commit(self, **_kwargs):
            operations.append("store_replay")
            return True

        async def mark_connected(self, **_kwargs):
            return True

        async def mark_heartbeat(self, **_kwargs):
            return True

        async def release_lease(self, **_kwargs):
            return True

    class FakeProcessor:
        async def process(self, *, record_id: str, **_kwargs):
            operations.append(f"process:{record_id}")

    class FakePubSubClient:
        async def subscribe(self, **kwargs):
            subscriptions.append(kwargs)
            yield SalesforcePubSubEvent(
                replay_id=b"\x00\x01",
                schema_id="schema-1",
                payload={
                    "ChangeEventHeader": {
                        "entityName": "Opportunity",
                        "recordIds": [
                            "006xx0000000001AAA",
                            "006xx0000000002AAA",
                        ],
                        "changeType": "UPDATE",
                    }
                },
            )
            stop.set()

    connector = SalesforceAggregateConnector(
        state_service=FakeStateService(),
        processor=FakeProcessor(),
        pubsub_factory=lambda _endpoint: FakePubSubClient(),
        token_provider=lambda _integration: SimpleNamespace(access_token="token"),
        session_factory=_session_factory,
    )
    integration = _integration_ref()

    await connector.run(integration, stop)

    assert operations == [
        "process:006xx0000000001AAA",
        "process:006xx0000000002AAA",
        "store_replay",
    ]
    assert subscriptions[0]["channel"] == "/data/ChangeEvents"
    assert subscriptions[0]["start_from_earliest"] is True


@pytest.mark.asyncio
async def test_connector_uses_cursor_instead_of_earliest_preset() -> None:
    state = _StateService(replay_id="AAE=", replay_preset="EARLIEST")
    stop = asyncio.Event()
    subscriptions: list[dict[str, object]] = []

    class FakePubSubClient:
        async def subscribe(self, **kwargs):
            subscriptions.append(kwargs)
            stop.set()
            if False:
                yield None

    await SalesforceAggregateConnector(
        state_service=state,
        pubsub_factory=lambda _endpoint: FakePubSubClient(),
        token_provider=lambda _integration: object(),
        session_factory=_session_factory,
    ).run(_integration_ref(), stop)

    assert subscriptions[0]["replay_id"] == b"\x00\x01"
    assert subscriptions[0]["start_from_earliest"] is False


@pytest.mark.asyncio
async def test_keepalive_stores_cursor_and_heartbeat_without_processing_record() -> None:
    state = _StateService()
    stop = asyncio.Event()

    class FailProcessor:
        async def process(self, **_kwargs):
            raise AssertionError("keepalive must not process a record")

    class FakePubSubClient:
        async def subscribe(self, **_kwargs):
            yield SalesforcePubSubKeepalive(replay_id=b"\x00\x02")
            stop.set()

    await SalesforceAggregateConnector(
        state_service=state,
        processor=FailProcessor(),
        pubsub_factory=lambda _endpoint: FakePubSubClient(),
        token_provider=lambda _integration: object(),
        session_factory=_session_factory,
    ).run(_integration_ref(), stop)

    assert "store_keepalive" in state.calls
    assert "heartbeat" in state.calls
    assert "store_replay" not in state.calls


@pytest.mark.asyncio
async def test_failed_record_prevents_replay_advancement() -> None:
    state = _StateService()
    stop = asyncio.Event()

    class FailProcessor:
        async def process(self, **_kwargs):
            raise RuntimeError("write failed")

    class FakePubSubClient:
        async def subscribe(self, **_kwargs):
            yield SalesforcePubSubEvent(
                replay_id=b"\x00\x03",
                schema_id="schema-1",
                payload={
                    "ChangeEventHeader": {
                        "entityName": "Opportunity",
                        "recordIds": ["006xx0000000001AAA"],
                        "changeType": "UPDATE",
                    }
                },
            )

    async def stop_after_backoff(_delay: float) -> None:
        stop.set()

    await SalesforceAggregateConnector(
        state_service=state,
        processor=FailProcessor(),
        pubsub_factory=lambda _endpoint: FakePubSubClient(),
        token_provider=lambda _integration: object(),
        session_factory=_session_factory,
        sleep=stop_after_backoff,
        jitter=lambda: 0,
    ).run(_integration_ref(), stop)

    assert "store_replay" not in state.calls


@pytest.mark.asyncio
async def test_normal_session_exit_reconnects_inside_connector() -> None:
    state = _StateService()
    stop = asyncio.Event()
    subscriptions = 0

    class FakePubSubClient:
        async def subscribe(self, **_kwargs):
            nonlocal subscriptions
            subscriptions += 1
            if subscriptions == 2:
                stop.set()
            if False:
                yield None

    await SalesforceAggregateConnector(
        state_service=state,
        pubsub_factory=lambda _endpoint: FakePubSubClient(),
        token_provider=lambda _integration: object(),
        session_factory=_session_factory,
    ).run(_integration_ref(), stop)

    assert subscriptions == 2
    assert "disconnected" not in state.calls


@pytest.mark.asyncio
async def test_transport_failures_use_capped_exponential_backoff() -> None:
    state = _StateService()
    stop = asyncio.Event()
    delays: list[float] = []

    class FakePubSubClient:
        async def subscribe(self, **_kwargs):
            raise ConnectionError("transport unavailable")
            if False:
                yield None

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 7:
            stop.set()

    await SalesforceAggregateConnector(
        state_service=state,
        pubsub_factory=lambda _endpoint: FakePubSubClient(),
        token_provider=lambda _integration: object(),
        session_factory=_session_factory,
        sleep=capture_sleep,
        jitter=lambda: 0,
    ).run(_integration_ref(), stop)

    assert delays == [1, 2, 4, 8, 16, 32, 60]


@pytest.mark.asyncio
async def test_cancellation_releases_owned_lease() -> None:
    state = _StateService()
    subscribed = asyncio.Event()
    blocked = asyncio.Event()
    supervisor_stop = asyncio.Event()

    class FakePubSubClient:
        async def subscribe(self, **_kwargs):
            subscribed.set()
            await blocked.wait()
            if False:
                yield None

    task = asyncio.create_task(
        SalesforceAggregateConnector(
            state_service=state,
            pubsub_factory=lambda _endpoint: FakePubSubClient(),
            token_provider=lambda _integration: object(),
            session_factory=_session_factory,
        ).run(_integration_ref(), supervisor_stop)
    )
    await subscribed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.calls[-1] == "release"
    assert supervisor_stop.is_set() is False


@pytest.mark.asyncio
async def test_replay_expiry_publishes_one_repair_and_stops() -> None:
    state = _StateService(replay_id="AAE=")
    repairs: list[str] = []

    class FakePubSubClient:
        async def subscribe(self, **_kwargs):
            raise SalesforcePubSubReplayIdError("outside retention")
            if False:
                yield None

    async def publish_repair(integration: SalesforceIntegrationRef) -> None:
        repairs.append(str(integration.integration_id))

    integration = _integration_ref()
    await SalesforceAggregateConnector(
        state_service=state,
        pubsub_factory=lambda _endpoint: FakePubSubClient(),
        token_provider=lambda _integration: object(),
        session_factory=_session_factory,
        repair_publisher=publish_repair,
    ).run(integration, asyncio.Event())

    assert repairs == [str(integration.integration_id)]
    assert state.calls.count("repairing") == 1
    assert state.calls[-1] == "release"
