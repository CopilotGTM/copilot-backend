"""Discover and reconcile up to seven Salesforce aggregate connectors in one event loop."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.observability.metrics import record_salesforce_cdc_capacity
from src.service.integrations.webhooks.salesforce.metadata import (
    AGGREGATE_CDC_CHANNEL,
    SALESFORCE_CDC_OBJECTS,
)
from src.service.integrations.webhooks.salesforce.subscription_state import (
    SalesforceCdcSubscriptionStateService,
)
from src.workers.salesforce_cdc_worker.config import SalesforceCdcWorkerSettings
from src.workers.salesforce_cdc_worker.connector import (
    SalesforceAggregateConnector,
    SalesforceIntegrationRef,
)

logger = logging.getLogger(__name__)

IntegrationKey = tuple[UUID, UUID]
Discover = Callable[
    [], Iterable[SalesforceIntegrationRef] | Awaitable[Iterable[SalesforceIntegrationRef]]
]
IntegrationCheck = Callable[[SalesforceIntegrationRef], bool | Awaitable[bool]]
IntegrationAction = Callable[[SalesforceIntegrationRef], Any | Awaitable[Any]]


class SalesforceCdcSupervisor:
    """Keep stable customer slots and isolate connector lifecycle failures."""

    def __init__(
        self,
        settings: SalesforceCdcWorkerSettings,
        *,
        discover: Discover | None = None,
        connector_factory: Callable[[], SalesforceAggregateConnector | Any] | None = None,
        has_active_legacy_leases: IntegrationCheck | None = None,
        mark_capacity_exceeded: IntegrationAction | None = None,
    ) -> None:
        """Accept discovery and state boundaries so reconciliation is deterministic in tests."""
        self.settings = settings
        self._discover = discover or discover_salesforce_integrations
        self._connector_factory = connector_factory or (
            lambda: SalesforceAggregateConnector(
                lease_seconds=settings.lease_seconds,
                lease_renew_seconds=settings.lease_renew_seconds,
                session_seconds=settings.stream_runtime_seconds,
                reconnect_max_seconds=settings.reconnect_max_seconds,
            )
        )
        self._has_active_legacy_leases = has_active_legacy_leases or _has_active_legacy_leases
        self._mark_capacity_exceeded = mark_capacity_exceeded or _mark_capacity_exceeded
        self._tasks: dict[IntegrationKey, asyncio.Task[None]] = {}
        self._integrations: dict[IntegrationKey, SalesforceIntegrationRef] = {}
        self._stop = asyncio.Event()
        self.last_discovered_at: datetime | None = None
        self.desired_count = 0
        self.capacity_exceeded_count = 0

    @property
    def running_keys(self) -> frozenset[IntegrationKey]:
        """Expose only live connector keys for readiness and operational tests."""
        return frozenset(key for key, task in self._tasks.items() if not task.done())

    async def reconcile(self) -> None:
        """Retain existing slots, remove stale integrations, then fill free capacity."""
        discovered = self._discover()
        if inspect.isawaitable(discovered):
            discovered = await discovered
        desired = {_key(item): item for item in discovered}
        self.last_discovered_at = datetime.now(UTC)
        self.desired_count = len(desired)
        self.capacity_exceeded_count = 0
        logger.info(
            "Salesforce aggregate CDC discovery completed",
            extra={"event": "salesforce_cdc_discovery", "desired": self.desired_count},
        )

        for key in list(self._tasks):
            task = self._tasks[key]
            if task.done():
                self._report_result(key, task)
                self._tasks.pop(key)
                self._integrations.pop(key, None)
            elif key not in desired:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._tasks.pop(key)
                self._integrations.pop(key, None)

        available = self.settings.max_integrations - len(self.running_keys)
        pending = [item for key, item in desired.items() if key not in self._tasks]
        for integration in pending:
            if await _maybe_await(self._has_active_legacy_leases(integration)):
                continue
            if available <= 0:
                await _maybe_await(self._mark_capacity_exceeded(integration))
                self.capacity_exceeded_count += 1
                logger.error(
                    "Salesforce aggregate CDC capacity exceeded",
                    extra={
                        "event": "salesforce_cdc_capacity_exceeded",
                        "organisation_id": str(integration.organisation_id),
                        "integration_id": str(integration.integration_id),
                        "capacity": self.settings.max_integrations,
                    },
                )
                continue
            key = _key(integration)
            connector = self._connector_factory()
            task = asyncio.create_task(
                connector.run(integration, self._stop),
                name=f"salesforce-cdc-{integration.integration_id}",
            )
            self._tasks[key] = task
            self._integrations[key] = integration
            available -= 1

        record_salesforce_cdc_capacity(self.desired_count, outcome="desired")
        record_salesforce_cdc_capacity(len(self.running_keys), outcome="running")
        record_salesforce_cdc_capacity(self.capacity_exceeded_count, outcome="capacity_exceeded")

    def readiness_state(self) -> dict[str, Any]:
        """Return the JSON-safe process state consumed by the readiness probe."""
        return {
            "discovered_at": (
                self.last_discovered_at.isoformat() if self.last_discovered_at else None
            ),
            "desired": self.desired_count,
            "running": len(self.running_keys),
            "capacity_exceeded": self.capacity_exceeded_count,
        }

    async def shutdown(self) -> None:
        """Cancel connectors concurrently and enforce the configured 90-second ceiling."""
        self._stop.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            async with asyncio.timeout(self.settings.shutdown_seconds):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._integrations.clear()

    def request_shutdown(self) -> None:
        """Signal the run loop from a process signal handler without doing async work there."""
        self._stop.set()

    async def run(self) -> None:
        """Reconcile continuously until process shutdown is requested."""
        try:
            while not self._stop.is_set():
                await self.reconcile()
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.discovery_seconds
                    )
        finally:
            await self.shutdown()

    def _report_result(self, key: IntegrationKey, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Salesforce connector exited unexpectedly and will be restarted",
                exc_info=error,
                extra={"organisation_id": str(key[0]), "integration_id": str(key[1])},
            )


async def discover_salesforce_integrations() -> list[SalesforceIntegrationRef]:
    """Return detached aggregate-ready integrations using master then tenant sessions."""
    organisation_ids: list[UUID] = []
    async for session in acquire_master_async_session():
        stmt = select(col(Organisation.organisation_id)).where(
            col(Organisation.status) == OrganisationStatus.APPROVED
        )
        organisation_ids = [row[0] for row in (await session.execute(stmt)).all()]

    discovered: list[tuple[datetime, SalesforceIntegrationRef]] = []
    for organisation_id in organisation_ids:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_type) == IntegrationType.SALESFORCE,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            rows = (await session.execute(stmt)).scalars().all()
            for integration in rows:
                ref = _integration_ref(integration)
                if ref is not None:
                    discovered.append((integration.created_at, ref))

    discovered.sort(key=lambda item: (item[0], item[1].organisation_id, item[1].integration_id))
    return [item[1] for item in discovered]


def _integration_ref(integration: KnowledgeIntegration) -> SalesforceIntegrationRef | None:
    metadata = dict(integration.connection_metadata or {})
    cdc = dict(metadata.get("salesforce_cdc") or {})
    if metadata.get("initial_sync_status") != "success" or cdc.get("mode") != "aggregate":
        return None
    if dict(cdc.get("aggregate") or {}).get("status") == "repairing":
        return None
    credentials = dict(integration.credentials or {})
    instance_url = str(
        credentials.get("instance_url") or metadata.get("salesforce_instance_url") or ""
    ).rstrip("/")
    salesforce_organization_id = str(metadata.get("salesforce_organization_id") or "").strip()
    if not instance_url or not salesforce_organization_id:
        return None
    enabled_entities = frozenset(
        config["object_name"]
        for key, config in SALESFORCE_CDC_OBJECTS.items()
        if dict(cdc.get(key) or {}).get("enabled") is True
    )
    return SalesforceIntegrationRef(
        organisation_id=integration.organisation_id,
        integration_id=integration.integration_id,
        instance_url=instance_url,
        salesforce_organization_id=salesforce_organization_id,
        pubsub_endpoint=str(
            metadata.get("salesforce_pubsub_endpoint") or "api.pubsub.salesforce.com:443"
        ),
        enabled_entities=enabled_entities,
    )


async def _has_active_legacy_leases(integration: SalesforceIntegrationRef) -> bool:
    service = SalesforceCdcSubscriptionStateService()
    async for session in acquire_org_async_session(integration.organisation_id):
        return await service.has_active_legacy_leases(
            session=session,
            organisation_id=integration.organisation_id,
            integration_id=integration.integration_id,
        )
    return True


async def _mark_capacity_exceeded(integration: SalesforceIntegrationRef) -> None:
    service = SalesforceCdcSubscriptionStateService()
    async for session in acquire_org_async_session(integration.organisation_id):
        await service.mark_capacity_exceeded(
            session=session,
            organisation_id=integration.organisation_id,
            integration_id=integration.integration_id,
            channel=AGGREGATE_CDC_CHANNEL,
        )
        return


def _key(integration: SalesforceIntegrationRef) -> IntegrationKey:
    return integration.organisation_id, integration.integration_id


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
