"""Maintain one replay-safe Salesforce aggregate CDC stream per integration."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.observability.metrics import (
    record_salesforce_cdc_connection,
    record_salesforce_cdc_event,
    record_salesforce_cdc_reconnect_delay,
    record_salesforce_cdc_record,
    record_salesforce_cdc_repair,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)
from src.service.integrations.webhooks.salesforce.change_processor import (
    SalesforceCdcChangeProcessor,
)
from src.service.integrations.webhooks.salesforce.decoder import decode_salesforce_change_event
from src.service.integrations.webhooks.salesforce.metadata import AGGREGATE_CDC_CHANNEL
from src.service.integrations.webhooks.salesforce.replay import decode_replay_id, encode_replay_id
from src.service.integrations.webhooks.salesforce.subscription_state import (
    SalesforceCdcSubscriptionStateService,
)
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    SalesforcePubSubClient,
    SalesforcePubSubKeepalive,
    SalesforcePubSubReplayIdError,
)

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 120
_LEASE_RENEWAL_SECONDS = 30
_SESSION_SECONDS = 2700
_SUPPORTED_ENTITIES = frozenset(
    {
        "Opportunity",
        "Account",
        "Contact",
        "User",
        "Task",
        "ContentDocument",
        "ContentDocumentLink",
    }
)
_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 32, 60)

SessionFactory = Callable[[UUID], AsyncIterator[Any]]
TokenProvider = Callable[["SalesforceIntegrationRef"], Awaitable[IntegrationTokenInfo] | Any]
RepairPublisher = Callable[["SalesforceIntegrationRef"], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class SalesforceIntegrationRef:
    """Connection metadata needed while no tenant database session is open."""

    organisation_id: UUID
    integration_id: UUID
    instance_url: str
    salesforce_organization_id: str
    pubsub_endpoint: str
    enabled_entities: frozenset[str]


class SalesforceAggregateConnector:
    """Own the aggregate lease and reconnect sessions until stopped or repair is needed."""

    def __init__(
        self,
        *,
        state_service: SalesforceCdcSubscriptionStateService | Any = None,
        processor: SalesforceCdcChangeProcessor | Any = None,
        pubsub_factory: Callable[[str], Any] | None = None,
        token_provider: TokenProvider | None = None,
        session_factory: SessionFactory = acquire_org_async_session,
        repair_publisher: RepairPublisher | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] | None = None,
        lease_seconds: int = _LEASE_SECONDS,
        lease_renew_seconds: int = _LEASE_RENEWAL_SECONDS,
        session_seconds: int = _SESSION_SECONDS,
        reconnect_max_seconds: int = 60,
    ) -> None:
        """Accept replaceable provider and persistence boundaries for deterministic tests."""
        self._state = state_service or SalesforceCdcSubscriptionStateService()
        self._processor = processor or SalesforceCdcChangeProcessor()
        self._pubsub_factory = pubsub_factory or (
            lambda endpoint: SalesforcePubSubClient(pubsub_endpoint=endpoint)
        )
        self._token_provider = token_provider or self._load_token
        self._session_factory = session_factory
        self._repair_publisher = repair_publisher or _publish_repair
        self._sleep = sleep
        self._jitter = jitter or (lambda: secrets.randbelow(501) / 1000)
        self._lease_seconds = lease_seconds
        self._lease_renew_seconds = lease_renew_seconds
        self._session_seconds = session_seconds
        self._reconnect_max_seconds = reconnect_max_seconds

    async def run(self, integration: SalesforceIntegrationRef, stop: asyncio.Event) -> None:
        """Maintain one replay-safe aggregate stream until stopped or repair is required."""
        lease_owner = str(uuid4())
        lease = await self._acquire_lease(integration, lease_owner)
        if not lease.acquired:
            return

        connector_task = asyncio.current_task()
        if connector_task is None:
            raise RuntimeError("Salesforce aggregate connector has no owning asyncio task")
        renewal = asyncio.create_task(
            self._renew_lease(integration, lease_owner, stop, connector_task),
            name=f"salesforce-cdc-lease-{integration.integration_id}",
        )
        replay_id = (
            decode_replay_id(lease.subscription.last_replay_id)
            if lease.subscription.last_replay_id
            else None
        )
        start_from_earliest = replay_id is None and lease.subscription.replay_preset == "EARLIEST"
        failures = 0

        try:
            while not stop.is_set():
                token_info = self._token_provider(integration)
                if inspect.isawaitable(token_info):
                    token_info = await token_info
                await self._mark_connected(integration, lease_owner)
                record_salesforce_cdc_connection("connected")
                logger.info(
                    "Salesforce aggregate CDC stream connected",
                    extra={
                        "event": "salesforce_cdc_connected",
                        "organisation_id": str(integration.organisation_id),
                        "integration_id": str(integration.integration_id),
                    },
                )
                try:
                    async for event in self._pubsub_factory(integration.pubsub_endpoint).subscribe(
                        token_info=token_info,
                        instance_url=integration.instance_url,
                        salesforce_organization_id=integration.salesforce_organization_id,
                        channel=AGGREGATE_CDC_CHANNEL,
                        replay_id=replay_id,
                        max_runtime_seconds=self._session_seconds,
                        start_from_earliest=start_from_earliest,
                    ):
                        if stop.is_set():
                            break
                        if isinstance(event, SalesforcePubSubKeepalive):
                            replay_id = event.replay_id
                            await self._store_keepalive(
                                integration, lease_owner, encode_replay_id(replay_id)
                            )
                            continue

                        change_event = decode_salesforce_change_event(
                            replay_id=event.replay_id,
                            schema_id=event.schema_id,
                            payload=event.payload,
                            object_name=None,
                            id_prefix=None,
                        )
                        if (
                            change_event.entity_name in _SUPPORTED_ENTITIES
                            and change_event.entity_name in integration.enabled_entities
                        ):
                            record_salesforce_cdc_event(
                                "processed", entity=change_event.entity_name
                            )
                            for record_id in change_event.record_ids:
                                try:
                                    await self._processor.process(
                                        organisation_id=integration.organisation_id,
                                        integration_id=integration.integration_id,
                                        entity_name=change_event.entity_name,
                                        record_id=record_id,
                                        change_type=change_event.change_type,
                                        instance_url=integration.instance_url,
                                    )
                                except Exception:
                                    record_salesforce_cdc_record(
                                        "failed",
                                        entity=change_event.entity_name,
                                        reason="processing",
                                    )
                                    raise
                                record_salesforce_cdc_record(
                                    "succeeded", entity=change_event.entity_name
                                )
                        else:
                            record_salesforce_cdc_event("skipped", entity=change_event.entity_name)
                            logger.warning(
                                "Skipping disabled or unsupported Salesforce CDC entity",
                                extra={"entity_name": change_event.entity_name},
                            )
                        replay_id = event.replay_id
                        start_from_earliest = False
                        await self._store_replay(
                            integration, lease_owner, encode_replay_id(replay_id)
                        )
                        logger.info(
                            "Salesforce aggregate CDC replay committed",
                            extra={
                                "event": "salesforce_cdc_replay_committed",
                                "organisation_id": str(integration.organisation_id),
                                "integration_id": str(integration.integration_id),
                                "entity": change_event.entity_name,
                            },
                        )
                        failures = 0
                except SalesforcePubSubReplayIdError:
                    if await self._mark_repairing(integration):
                        record_salesforce_cdc_repair("started", reason="replay_expired")
                        logger.error(
                            "Salesforce aggregate CDC replay expired; serial repair started",
                            extra={
                                "event": "salesforce_cdc_repair_started",
                                "organisation_id": str(integration.organisation_id),
                                "integration_id": str(integration.integration_id),
                            },
                        )
                        await self._repair_publisher(integration)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._mark_disconnected(integration, lease_owner)
                    delay = min(
                        _BACKOFF_SECONDS[min(failures, len(_BACKOFF_SECONDS) - 1)],
                        self._reconnect_max_seconds,
                    )
                    failures += 1
                    reconnect_delay = delay + self._jitter()
                    record_salesforce_cdc_connection("disconnected")
                    record_salesforce_cdc_reconnect_delay(reconnect_delay, reason="transport")
                    logger.exception(
                        "Salesforce aggregate CDC stream disconnected",
                        extra={
                            "event": "salesforce_cdc_disconnected",
                            "organisation_id": str(integration.organisation_id),
                            "integration_id": str(integration.integration_id),
                            "reason": "transport",
                            "reconnect_delay_seconds": reconnect_delay,
                        },
                    )
                    await self._sleep(reconnect_delay)
                    continue

                # A 2,700-second provider session ending normally is rotation, not failure.
                record_salesforce_cdc_connection("disconnected")
                logger.info(
                    "Salesforce aggregate CDC session rotated",
                    extra={
                        "event": "salesforce_cdc_disconnected",
                        "organisation_id": str(integration.organisation_id),
                        "integration_id": str(integration.integration_id),
                        "reason": "session_rotation",
                    },
                )
                failures = 0
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
            await self._release_lease(integration, lease_owner)

    async def _acquire_lease(self, integration: SalesforceIntegrationRef, owner: str) -> Any:
        async for session in self._session_factory(integration.organisation_id):
            return await self._state.acquire_lease(
                session=session,
                organisation_id=integration.organisation_id,
                integration_id=integration.integration_id,
                channel=AGGREGATE_CDC_CHANNEL,
                salesforce_organization_id=integration.salesforce_organization_id,
                lease_owner=owner,
                lease_seconds=self._lease_seconds,
                pubsub_endpoint=integration.pubsub_endpoint,
            )
        raise RuntimeError("Organisation session factory did not yield")

    async def _renew_lease(
        self,
        integration: SalesforceIntegrationRef,
        owner: str,
        stop: asyncio.Event,
        connector_task: asyncio.Task[Any],
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._lease_renew_seconds)
                return
            except TimeoutError:
                pass
            async for session in self._session_factory(integration.organisation_id):
                renewed = await self._state.renew_lease(
                    session=session,
                    organisation_id=integration.organisation_id,
                    integration_id=integration.integration_id,
                    channel=AGGREGATE_CDC_CHANNEL,
                    lease_owner=owner,
                    lease_seconds=self._lease_seconds,
                )
                if not renewed:
                    connector_task.cancel()
                    return
                break

    async def _state_call(
        self, integration: SalesforceIntegrationRef, method: str, **kwargs: Any
    ) -> Any:
        async for session in self._session_factory(integration.organisation_id):
            return await getattr(self._state, method)(
                session=session,
                organisation_id=integration.organisation_id,
                integration_id=integration.integration_id,
                channel=AGGREGATE_CDC_CHANNEL,
                **kwargs,
            )
        raise RuntimeError("Organisation session factory did not yield")

    async def _mark_connected(self, integration: SalesforceIntegrationRef, owner: str) -> None:
        if not await self._state_call(integration, "mark_connected", lease_owner=owner):
            raise RuntimeError("Salesforce aggregate connector lost its lease")

    async def _mark_disconnected(self, integration: SalesforceIntegrationRef, owner: str) -> None:
        if not await self._state_call(integration, "mark_disconnected", lease_owner=owner):
            raise RuntimeError("Salesforce aggregate connector lost its lease")

    async def _store_keepalive(
        self, integration: SalesforceIntegrationRef, owner: str, replay_id: str
    ) -> None:
        stored = await self._state_call(
            integration, "store_keepalive_replay", lease_owner=owner, replay_id=replay_id
        )
        heartbeat = await self._state_call(integration, "mark_heartbeat", lease_owner=owner)
        if not stored or not heartbeat:
            raise RuntimeError("Salesforce aggregate connector lost its lease")

    async def _store_replay(
        self, integration: SalesforceIntegrationRef, owner: str, replay_id: str
    ) -> None:
        stored = await self._state_call(
            integration, "store_replay_after_commit", lease_owner=owner, replay_id=replay_id
        )
        heartbeat = await self._state_call(integration, "mark_heartbeat", lease_owner=owner)
        if not stored or not heartbeat:
            raise RuntimeError("Salesforce aggregate connector lost its lease")

    async def _mark_repairing(self, integration: SalesforceIntegrationRef) -> bool:
        return bool(await self._state_call(integration, "mark_repairing"))

    async def _release_lease(self, integration: SalesforceIntegrationRef, owner: str) -> None:
        await self._state_call(integration, "release_lease", lease_owner=owner)

    async def _load_token(self, integration: SalesforceIntegrationRef) -> IntegrationTokenInfo:
        """Load tenant credentials briefly, then refresh with no database session held."""
        token_info: IntegrationTokenInfo | None = None
        async for session in self._session_factory(integration.organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == integration.organisation_id,
                col(KnowledgeIntegration.integration_id) == integration.integration_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise ValueError(f"Salesforce integration {integration.integration_id} not found")
            token_info = IntegrationTokenInfo.from_integration(row, integration.organisation_id)
            break
        if token_info is None:
            raise RuntimeError("Organisation session factory did not yield")
        token_info.access_token = await get_access_token(token_info)
        return token_info


async def _publish_repair(integration: SalesforceIntegrationRef) -> None:
    """Publish the one serial aggregate repair task without importing a Celery application."""
    # copilotgtm: Keep Celery out of the long-lived worker's startup graph; replay expiry is the
    # only path that needs canvas publication, and it is already fenced as single-flight.
    from src.service.integrations.crm.orchestration.salesforce_cdc_repair_chain import (
        publish_salesforce_cdc_repair_chain,
    )

    await publish_salesforce_cdc_repair_chain(
        integration.organisation_id,
        integration.integration_id,
    )
