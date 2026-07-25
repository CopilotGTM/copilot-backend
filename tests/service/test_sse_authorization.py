"""Authorization boundaries for organisation-wide SSE status channels."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.service.chat.chat_attachments_sse_service import AttachmentSSEService
from src.service.crm.deal_assets_sse_service import DealAssetSSEService
from src.service.knowledge_base.knowledge_sse_service import KnowledgeSourceSSEService


class _PollingPubSub:
    def __init__(self, messages: list[dict | None]) -> None:
        self._messages = messages

    async def subscribe_and_poll(self, _channel: str, poll_timeout: float = 1.0):
        del poll_timeout
        for message in self._messages:
            yield message

    async def close(self) -> None:
        return None


class _FakeStorage:
    async def close(self) -> None:
        return None


def _status_ids(events: list[dict], id_field: str) -> list[str]:
    return [
        item[id_field]
        for event in events
        if event["event"] == "status"
        for item in json.loads(event["data"])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_unowned_id", [False, True])
async def test_knowledge_stream_only_forwards_database_authorized_sources(
    include_unowned_id: bool,
) -> None:
    organisation_id = uuid4()
    user_id = uuid4()
    owned_id = uuid4()
    unowned_id = uuid4()
    explicit_ids: set[UUID] | None = {owned_id, unowned_id} if include_unowned_id else None

    service = KnowledgeSourceSSEService.__new__(KnowledgeSourceSSEService)
    service.organisation_id = organisation_id
    service.user_id = user_id
    service.redis_pubsub = _PollingPubSub(
        [
            {"source_id": str(unowned_id), "status": "completed"},
            {"source_id": str(owned_id), "status": "completed"},
        ]
    )
    service.storage = _FakeStorage()
    service.get_initial_sources = AsyncMock(return_value=[SimpleNamespace(source_id=owned_id)])
    service.serialize_source = AsyncMock(
        return_value={"source_id": str(owned_id), "status": "extracting"}
    )

    events = [
        event
        async for event in service.stream_events(
            explicit_ids=explicit_ids,
            timeout_seconds=1,
            heartbeat_interval=1,
        )
    ]

    assert _status_ids(events, "source_id") == [str(owned_id), str(owned_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_unowned_id", [False, True])
async def test_attachment_stream_only_forwards_database_authorized_attachments(
    include_unowned_id: bool,
) -> None:
    organisation_id = uuid4()
    user_id = uuid4()
    owned_id = uuid4()
    unowned_id = uuid4()
    explicit_ids: set[UUID] | None = {owned_id, unowned_id} if include_unowned_id else None

    service = AttachmentSSEService.__new__(AttachmentSSEService)
    service.organisation_id = organisation_id
    service.user_id = user_id
    service.redis_pubsub = _PollingPubSub(
        [
            {"attachment_id": str(unowned_id), "status": "completed"},
            {"attachment_id": str(owned_id), "status": "completed"},
        ]
    )
    service.storage = _FakeStorage()
    service.get_initial_attachments = AsyncMock(
        return_value=[SimpleNamespace(attachment_id=owned_id)]
    )
    service.serialize_attachment = AsyncMock(
        return_value={"attachment_id": str(owned_id), "status": "processing"}
    )

    events = [
        event
        async for event in service.stream_events(
            explicit_ids=explicit_ids,
            timeout_seconds=1,
            heartbeat_interval=1,
        )
    ]

    assert _status_ids(events, "attachment_id") == [
        str(owned_id),
        str(owned_id),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("service_type", ["knowledge", "attachment"])
async def test_idle_status_streams_emit_heartbeat_without_redis_message(service_type: str) -> None:
    owned_id = uuid4()
    organisation_id = uuid4()
    user_id = uuid4()
    redis_pubsub = _PollingPubSub(
        [
            None,
            {
                "source_id" if service_type == "knowledge" else "attachment_id": str(owned_id),
                "status": "completed",
            },
        ]
    )

    if service_type == "knowledge":
        service = KnowledgeSourceSSEService.__new__(KnowledgeSourceSSEService)
        service.get_initial_sources = AsyncMock(return_value=[SimpleNamespace(source_id=owned_id)])
        service.serialize_source = AsyncMock(
            return_value={"source_id": str(owned_id), "status": "extracting"}
        )
    else:
        service = AttachmentSSEService.__new__(AttachmentSSEService)
        service.get_initial_attachments = AsyncMock(
            return_value=[SimpleNamespace(attachment_id=owned_id)]
        )
        service.serialize_attachment = AsyncMock(
            return_value={"attachment_id": str(owned_id), "status": "processing"}
        )

    service.organisation_id = organisation_id
    service.user_id = user_id
    service.redis_pubsub = redis_pubsub
    service.storage = _FakeStorage()

    events = [
        event
        async for event in service.stream_events(
            timeout_seconds=1,
            heartbeat_interval=0,
        )
    ]

    assert "ping" in [event["event"] for event in events]


@pytest.mark.asyncio
async def test_idle_deal_asset_stream_emits_heartbeat_without_redis_message() -> None:
    service = DealAssetSSEService(uuid4())
    service.redis_pubsub = _PollingPubSub([None, {"deal_id": str(uuid4()), "status": "active"}])

    events = [
        event
        async for event in service.stream_events(
            timeout_seconds=1,
            heartbeat_interval=0,
        )
    ]

    assert [event["event"] for event in events] == ["ping", "ping", "status"]
