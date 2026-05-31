from __future__ import annotations

import json
import time
from asyncio import Queue
from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol, cast

from fastavro import parse_schema, schemaless_reader

from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_schema_cache import (
    SalesforcePubSubSchemaCache,
    SalesforcePubSubSchemaClient,
)

_DEFAULT_PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:443"


@dataclass(slots=True, frozen=True)
class SalesforcePubSubEvent:
    replay_id: bytes
    schema_id: str
    payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class SalesforcePubSubRawEvent:
    replay_id: bytes
    schema_id: str
    payload: bytes


@dataclass(slots=True, frozen=True)
class SalesforcePubSubKeepalive:
    replay_id: bytes


class SalesforcePubSubReplayIdError(RuntimeError):
    """Salesforce rejected the replay ID used to start the subscription."""


class SalesforcePubSubTransport(Protocol):
    def subscribe(
        self,
        *,
        channel: str,
        replay_id: bytes | None,
        metadata: tuple[tuple[str, str], ...],
        num_requested: int,
    ) -> AsyncIterator[Any]: ...


class SalesforceGrpcSchemaClient(SalesforcePubSubSchemaClient):
    def __init__(self, *, pubsub_endpoint: str = _DEFAULT_PUBSUB_ENDPOINT) -> None:
        self._pubsub_endpoint = pubsub_endpoint

    async def get_schema(self, schema_id: str, *, metadata: tuple[tuple[str, str], ...]) -> str:
        from grpc import aio

        from src.workers.integrations_worker.providers.crm.salesforce.pubsub_proto import (
            pubsub_api_pb2,
            pubsub_api_pb2_grpc,
        )

        pb2 = cast(Any, pubsub_api_pb2)
        channel = aio.secure_channel(self._pubsub_endpoint, aio.ssl_channel_credentials())
        try:
            stub = pubsub_api_pb2_grpc.PubSubStub(channel)
            response = await stub.GetSchema(
                pb2.SchemaRequest(schema_id=schema_id),
                metadata=metadata,
            )
            return str(response.schema_json)
        finally:
            await channel.close()


class SalesforceGrpcPubSubTransport:
    def __init__(self, *, pubsub_endpoint: str = _DEFAULT_PUBSUB_ENDPOINT) -> None:
        self._pubsub_endpoint = pubsub_endpoint

    async def subscribe(
        self,
        *,
        channel: str,
        replay_id: bytes | None,
        metadata: tuple[tuple[str, str], ...],
        num_requested: int,
    ) -> AsyncIterator[SalesforcePubSubRawEvent | SalesforcePubSubKeepalive]:
        from grpc import aio

        from src.workers.integrations_worker.providers.crm.salesforce.pubsub_proto import (
            pubsub_api_pb2,
            pubsub_api_pb2_grpc,
        )

        pb2 = cast(Any, pubsub_api_pb2)
        request_queue: Queue[Any] = Queue()
        replay_preset = _replay_preset_for(replay_id, pb2)
        await request_queue.put(
            pb2.FetchRequest(
                topic_name=channel,
                replay_preset=replay_preset,
                replay_id=replay_id or b"",
                num_requested=num_requested,
            )
        )

        async def requests() -> AsyncIterator[Any]:
            while True:
                request = await request_queue.get()
                if request is None:
                    return
                yield request

        grpc_channel = aio.secure_channel(self._pubsub_endpoint, aio.ssl_channel_credentials())
        try:
            stub = pubsub_api_pb2_grpc.PubSubStub(grpc_channel)
            responses = stub.Subscribe(requests(), metadata=metadata)
            async for response in responses:
                for consumer_event in response.events:
                    yield SalesforcePubSubRawEvent(
                        replay_id=bytes(consumer_event.replay_id),
                        schema_id=str(consumer_event.event.schema_id),
                        payload=bytes(consumer_event.event.payload),
                    )
                if not response.events and response.latest_replay_id:
                    yield SalesforcePubSubKeepalive(replay_id=bytes(response.latest_replay_id))
                next_request_count = _next_fetch_request_count(
                    pending_num_requested=int(response.pending_num_requested),
                    num_requested=num_requested,
                )
                if next_request_count > 0:
                    await request_queue.put(pb2.FetchRequest(num_requested=next_request_count))
        except aio.AioRpcError as exc:
            if _is_replay_id_error(exc):
                raise SalesforcePubSubReplayIdError(str(exc)) from exc
            raise
        finally:
            await request_queue.put(None)
            await grpc_channel.close()


class SalesforcePubSubClient:
    def __init__(
        self,
        *,
        pubsub_endpoint: str = _DEFAULT_PUBSUB_ENDPOINT,
        transport: SalesforcePubSubTransport | None = None,
        schema_cache: SalesforcePubSubSchemaCache | None = None,
    ) -> None:
        self._transport = transport or SalesforceGrpcPubSubTransport(
            pubsub_endpoint=pubsub_endpoint
        )
        self._schema_cache = schema_cache or SalesforcePubSubSchemaCache(
            SalesforceGrpcSchemaClient(pubsub_endpoint=pubsub_endpoint)
        )

    async def subscribe(
        self,
        *,
        token_info: IntegrationTokenInfo,
        instance_url: str,
        salesforce_organization_id: str,
        channel: str,
        replay_id: bytes | None,
        max_runtime_seconds: int,
    ) -> AsyncIterator[SalesforcePubSubEvent | SalesforcePubSubKeepalive]:
        if self._transport is None:
            raise RuntimeError("Salesforce Pub/Sub transport is not configured")

        if not token_info.access_token:
            raise RuntimeError("Salesforce Pub/Sub subscription requires an access token")

        metadata = (
            ("accesstoken", token_info.access_token),
            ("instanceurl", instance_url),
            ("tenantid", salesforce_organization_id),
        )
        deadline = time.monotonic() + max_runtime_seconds
        async for raw_event in self._transport.subscribe(
            channel=channel,
            replay_id=replay_id,
            metadata=metadata,
            num_requested=10,
        ):
            if time.monotonic() >= deadline:
                break
            event = await self._decode_event(raw_event, metadata=metadata)
            yield event

    async def _decode_event(
        self,
        raw_event: Any,
        *,
        metadata: tuple[tuple[str, str], ...],
    ) -> SalesforcePubSubEvent | SalesforcePubSubKeepalive:
        if isinstance(raw_event, SalesforcePubSubEvent | SalesforcePubSubKeepalive):
            return raw_event
        schema_id = str(raw_event.schema_id)
        schema_json = await self._schema_cache.get_schema(schema_id, metadata=metadata)
        schema = parse_schema(json.loads(schema_json))
        payload = schemaless_reader(BytesIO(raw_event.payload), schema)
        if not isinstance(payload, dict):
            raise ValueError("Salesforce Pub/Sub Avro payload decoded to non-dict")
        return SalesforcePubSubEvent(
            replay_id=bytes(raw_event.replay_id),
            schema_id=schema_id,
            payload=cast(dict[str, Any], payload),
        )


def _is_replay_id_error(exc: Exception) -> bool:
    message = str(exc).lower()
    details = getattr(exc, "details", lambda: "")()
    if "replayid" in message or "replay id" in message:
        return True
    if "replayid" in str(details).lower() or "replay id" in str(details).lower():
        return True
    trailing_metadata = getattr(exc, "trailing_metadata", lambda: None)()
    if trailing_metadata is None:
        return False
    for key, value in trailing_metadata:
        if key == "error-code" and "replayid" in str(value).lower():
            return True
    return False


def _replay_preset_for(replay_id: bytes | None, pb2: Any) -> Any:
    return pb2.ReplayPreset.CUSTOM if replay_id is not None else pb2.ReplayPreset.LATEST


def _next_fetch_request_count(*, pending_num_requested: int, num_requested: int) -> int:
    return max(num_requested - max(pending_num_requested, 0), 0)
