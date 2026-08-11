"""Exercise the real aggregate CDC supervisor and connectors with deterministic fakes."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

import psutil
from src.service.integrations.webhooks.salesforce.replay import decode_replay_id
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    SalesforcePubSubEvent,
    SalesforcePubSubKeepalive,
    SalesforcePubSubReplayIdError,
)
from src.workers.salesforce_cdc_worker.config import SalesforceCdcWorkerSettings
from src.workers.salesforce_cdc_worker.connector import (
    SalesforceAggregateConnector,
    SalesforceIntegrationRef,
)
from src.workers.salesforce_cdc_worker.supervisor import SalesforceCdcSupervisor

_NAMESPACE = UUID("76558ef6-5b8e-45df-bb21-c34d56e8f92f")


class _Harness:
    def __init__(self, customers: int, duration_seconds: float) -> None:
        self.customers = customers
        self.duration_seconds = duration_seconds
        self.started_at = time.monotonic()
        self.active_connectors = 0
        self.peak_connectors = 0
        self.connector_starts = 0
        self.active_sessions = 0
        self.peak_sessions = 0
        self.repair_count = 0
        self.expired_replays = 0
        self.replay_order_violations = 0
        self.processed: dict[int, int] = dict.fromkeys(range(customers), 0)
        self.events: dict[int, int] = dict.fromkeys(range(customers), 0)
        self.disconnects: dict[int, int] = dict.fromkeys(range(customers), 0)
        self.last_replay: dict[int, str | None] = dict.fromkeys(range(customers))
        self.disconnected_at: dict[int, float] = {}
        self.reconnect_latencies: list[float] = []
        self.integration_customer: dict[UUID, int] = {}
        self.task_count_high_water = 0
        self.task_count_baseline = 0
        self.rss_samples: list[tuple[float, int]] = []
        self.event_interval = 10.0 if duration_seconds > 60 else 0.5
        self.keepalive_interval = 240.0 if duration_seconds > 300 else 1.0
        self.disconnect_interval = (
            3600.0 if duration_seconds > 3600 else max(duration_seconds / 3, 2.0)
        )
        now = time.monotonic()
        self.next_event = dict.fromkeys(range(customers), now)
        self.next_keepalive = dict.fromkeys(range(customers), now + self.keepalive_interval)
        self.next_disconnect = dict.fromkeys(range(customers), now + self.disconnect_interval)

    def integrations(self) -> list[SalesforceIntegrationRef]:
        """Build stable detached tenant fixtures for supervisor discovery."""
        refs: list[SalesforceIntegrationRef] = []
        for customer in range(self.customers):
            organisation_id = uuid5(_NAMESPACE, f"organisation-{customer}")
            integration_id = uuid5(_NAMESPACE, f"integration-{customer}")
            self.integration_customer[integration_id] = customer
            refs.append(
                SalesforceIntegrationRef(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    instance_url=f"https://customer-{customer}.my.salesforce.com",
                    salesforce_organization_id=f"00D{customer:012d}",
                    pubsub_endpoint="fake.pubsub.salesforce.test:443",
                    enabled_entities=frozenset({"Opportunity"}),
                )
            )
        return refs

    async def session_factory(self, _organisation_id: UUID):
        """Track every fake tenant-session lifetime so leaks fail the gate."""
        self.active_sessions += 1
        self.peak_sessions = max(self.peak_sessions, self.active_sessions)
        try:
            yield object()
        finally:
            self.active_sessions -= 1

    async def token_provider(self, integration: SalesforceIntegrationRef) -> Any:
        """Resolve a fixture customer without opening a real provider session."""
        return SimpleNamespace(customer=self.integration_customer[integration.integration_id])

    async def repair_publisher(self, _integration: SalesforceIntegrationRef) -> None:
        """Count the replay-expiry repair publication without a broker."""
        self.repair_count += 1

    async def process(self, *, integration_id: UUID, record_id: str, **_kwargs: Any) -> None:
        """Record successful processing and make the final tenant deliberately slower."""
        customer = self.integration_customer[integration_id]
        if customer == self.customers - 1:
            await asyncio.sleep(min(self.event_interval * 0.8, 0.2))
        self.processed[customer] = max(self.processed[customer], int(record_id[3:]))

    def pubsub_factory(self, _endpoint: str) -> _FakePubSub:
        """Return a deterministic transport while retaining shared soak counters."""
        return _FakePubSub(self)


class _FakeState:
    def __init__(self, harness: _Harness) -> None:
        self.harness = harness

    async def acquire_lease(self, *, integration_id: UUID, **_kwargs: Any) -> Any:
        """Return the latest fake aggregate checkpoint under an acquired lease."""
        customer = self.harness.integration_customer[integration_id]
        return SimpleNamespace(
            acquired=True,
            subscription=SimpleNamespace(
                last_replay_id=self.harness.last_replay[customer], replay_preset="EARLIEST"
            ),
        )

    async def store_replay_after_commit(
        self, *, integration_id: UUID, replay_id: str, **_kwargs: Any
    ) -> bool:
        """Fail the report if replay advances beyond successful processing."""
        customer = self.harness.integration_customer[integration_id]
        sequence = int.from_bytes(decode_replay_id(replay_id), "big")
        if sequence > self.harness.processed[customer]:
            self.harness.replay_order_violations += 1
        self.harness.last_replay[customer] = replay_id
        return True

    async def store_keepalive_replay(
        self, *, integration_id: UUID, replay_id: str, **_kwargs: Any
    ) -> bool:
        """Persist keepalive replay independently from data-event ordering."""
        customer = self.harness.integration_customer[integration_id]
        self.harness.last_replay[customer] = replay_id
        return True

    async def mark_repairing(self, **_kwargs: Any) -> bool:
        """Win the deterministic single replay-expiry repair fence."""
        return True

    async def __getattr_call(self, **_kwargs: Any) -> bool:
        return True

    renew_lease = __getattr_call
    mark_connected = __getattr_call
    mark_disconnected = __getattr_call
    mark_heartbeat = __getattr_call
    release_lease = __getattr_call


class _FakeProcessor:
    def __init__(self, harness: _Harness) -> None:
        self.harness = harness

    async def process(self, **kwargs: Any) -> None:
        """Delegate through the same processor interface used by the connector."""
        await self.harness.process(**kwargs)


class _FakePubSub:
    def __init__(self, harness: _Harness) -> None:
        self.harness = harness

    async def subscribe(self, *, token_info: Any, **_kwargs: Any):
        """Yield timed events, keepalives, replay expiry, and transport failures."""
        customer = token_info.customer
        harness = self.harness
        harness.connector_starts += 1
        harness.active_connectors += 1
        harness.peak_connectors = max(harness.peak_connectors, harness.active_connectors)
        reconnected_at = time.monotonic()
        if customer in harness.disconnected_at:
            harness.reconnect_latencies.append(
                reconnected_at - harness.disconnected_at.pop(customer)
            )
        try:
            if customer == 0 and harness.expired_replays == 0:
                await asyncio.sleep(min(1.0, max(0.5, harness.duration_seconds / 10)))
                harness.expired_replays += 1
                raise SalesforcePubSubReplayIdError("deterministic expired replay")

            while True:
                now = time.monotonic()
                if now >= harness.next_disconnect[customer]:
                    harness.disconnects[customer] += 1
                    harness.disconnected_at[customer] = now
                    harness.next_disconnect[customer] = now + harness.disconnect_interval
                    raise ConnectionError("deterministic transport disconnect")
                if now >= harness.next_keepalive[customer]:
                    harness.next_keepalive[customer] = now + harness.keepalive_interval
                    sequence = max(harness.events[customer], 1)
                    yield SalesforcePubSubKeepalive(replay_id=sequence.to_bytes(8, "big"))
                    continue
                if now >= harness.next_event[customer]:
                    harness.events[customer] += 1
                    sequence = harness.events[customer]
                    harness.next_event[customer] = now + harness.event_interval
                    yield SalesforcePubSubEvent(
                        replay_id=sequence.to_bytes(8, "big"),
                        schema_id="soak-schema",
                        payload={
                            "ChangeEventHeader": {
                                "entityName": "Opportunity",
                                "recordIds": [f"006{sequence:015d}"],
                                "changeType": "UPDATE",
                            }
                        },
                    )
                    continue
                await asyncio.sleep(min(0.05, harness.next_event[customer] - now))
        finally:
            harness.active_connectors -= 1


async def _monitor(harness: _Harness, stop: asyncio.Event) -> None:
    process = psutil.Process()
    while not stop.is_set():
        elapsed = time.monotonic() - harness.started_at
        harness.task_count_high_water = max(harness.task_count_high_water, len(asyncio.all_tasks()))
        harness.rss_samples.append((elapsed, process.memory_info().rss))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.2)


async def run_soak(*, customers: int, duration_seconds: float, output: Path) -> dict[str, Any]:
    """Run the deterministic soak and persist a machine-readable acceptance report."""
    if not 1 <= customers <= 7:
        raise ValueError("customers must be between 1 and 7")
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    harness = _Harness(customers, duration_seconds)
    harness.task_count_baseline = len(asyncio.all_tasks())
    integrations = harness.integrations()
    state = _FakeState(harness)
    processor = _FakeProcessor(harness)

    def _connector_factory() -> SalesforceAggregateConnector:
        return SalesforceAggregateConnector(
            state_service=state,
            processor=processor,
            pubsub_factory=harness.pubsub_factory,
            token_provider=harness.token_provider,
            session_factory=harness.session_factory,
            repair_publisher=harness.repair_publisher,
            reconnect_max_seconds=0.1,
            lease_seconds=max(duration_seconds + 5, 10),
            lease_renew_seconds=max(min(duration_seconds / 2, 5), 0.2),
            session_seconds=max(duration_seconds / 2, 0.5),
        )

    supervisor = SalesforceCdcSupervisor(
        SalesforceCdcWorkerSettings(
            max_integrations=customers,
            discovery_seconds=60 if duration_seconds > 60 else 0.25,
            stream_runtime_seconds=max(int(duration_seconds / 2), 1),
            lease_seconds=max(int(duration_seconds + 5), 10),
            lease_renew_seconds=1,
            reconnect_max_seconds=1,
            shutdown_seconds=10,
        ),
        discover=lambda: integrations,
        connector_factory=_connector_factory,
        has_active_legacy_leases=lambda _integration: False,
        mark_capacity_exceeded=lambda _integration: None,
    )
    monitor_stop = asyncio.Event()
    monitor = asyncio.create_task(
        _monitor(harness, monitor_stop), name="salesforce-cdc-soak-monitor"
    )
    supervisor_task = asyncio.create_task(supervisor.run(), name="salesforce-cdc-soak-supervisor")
    await asyncio.sleep(duration_seconds)
    supervisor.request_shutdown()
    await supervisor_task
    monitor_stop.set()
    await monitor
    await asyncio.sleep(0)

    process = psutil.Process()
    rss_values = [rss for _elapsed, rss in harness.rss_samples] or [process.memory_info().rss]
    warmup_cutoff = min(duration_seconds / 10, 3600)
    warmup_values = [rss for elapsed, rss in harness.rss_samples if elapsed >= warmup_cutoff]
    reconnect_max = max(harness.reconnect_latencies, default=0.0)
    final_task_count = len(asyncio.all_tasks())
    task_count_bound = harness.task_count_baseline + customers * 3 + 5
    event_counts = list(harness.events.values())
    healthy_event_counts = [
        count for customer, count in harness.events.items() if customer not in {0, customers - 1}
    ] or event_counts
    healthy_event_spread = max(healthy_event_counts) - min(healthy_event_counts)
    report: dict[str, Any] = {
        "customers": customers,
        "duration_seconds": duration_seconds,
        "processes": 1 + len(process.children(recursive=True)),
        "connector_starts": harness.connector_starts,
        "peak_connectors": harness.peak_connectors,
        "final_connectors": harness.active_connectors,
        "events_by_customer": {str(key): value for key, value in harness.events.items()},
        "healthy_event_spread": healthy_event_spread,
        "disconnects_by_customer": {str(key): value for key, value in harness.disconnects.items()},
        "reconnect_latencies_seconds": harness.reconnect_latencies,
        "max_reconnect_latency_seconds": reconnect_max,
        "expired_replays": harness.expired_replays,
        "repairs_published": harness.repair_count,
        "replay_order_violations": harness.replay_order_violations,
        "peak_rss_bytes": max(rss_values),
        "warmup_rss_bytes": warmup_values[0] if warmup_values else rss_values[0],
        "final_rss_bytes": rss_values[-1],
        "rss_growth_after_warmup_bytes": (
            rss_values[-1] - (warmup_values[0] if warmup_values else rss_values[0])
        ),
        "task_count_high_water": harness.task_count_high_water,
        "task_count_baseline": harness.task_count_baseline,
        "task_count_bound": task_count_bound,
        "final_task_count": final_task_count,
        "leaked_sessions": harness.active_sessions,
    }
    report["acceptance_passed"] = all(
        (
            report["processes"] == 1,
            report["peak_connectors"] == customers,
            all(count > 0 for count in harness.events.values()),
            reconnect_max <= 120,
            report["replay_order_violations"] == 0,
            healthy_event_spread <= 2,
            report["peak_rss_bytes"] < int(2.4 * 1024**3),
            report["rss_growth_after_warmup_bytes"] < 256 * 1024**2,
            report["task_count_high_water"] <= task_count_bound,
            report["final_task_count"] <= harness.task_count_baseline,
            report["leaked_sessions"] == 0,
            report["final_connectors"] == 0,
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_soak(
            customers=args.customers,
            duration_seconds=args.duration_seconds,
            output=args.output,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["acceptance_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
