from __future__ import annotations

import json
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import JobStatus
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService


@pytest.mark.asyncio
async def test_stream_events_access_denied_emits_error(monkeypatch) -> None:
    service = DealJobSSEService(uuid4(), uuid4(), uuid4())

    async def fake_get_job_status():
        return None, None, True

    monkeypatch.setattr(service, "_get_job_status", fake_get_job_status)

    events: list[dict] = []
    async for event in service.stream_events():
        events.append(event)

    assert events[0]["event"] == "ready"
    assert events[1]["event"] == "error"
    payload = json.loads(events[1]["data"])
    assert payload["reconnect"] is False


@pytest.mark.asyncio
async def test_stream_events_completed_emits_cached_result(monkeypatch) -> None:
    service = DealJobSSEService(uuid4(), uuid4(), uuid4())
    result_data = {"created_count": 2, "failed_count": 0}

    async def fake_get_job_status():
        return JobStatus.COMPLETED, result_data, False

    monkeypatch.setattr(service, "_get_job_status", fake_get_job_status)

    events: list[dict] = []
    async for event in service.stream_events():
        events.append(event)

    assert events[0]["event"] == "ready"
    assert events[1]["event"] == "frame"
    frame_payload = json.loads(events[1]["data"])
    assert frame_payload["content"] == result_data
    assert events[2]["event"] == "complete"


@pytest.mark.asyncio
async def test_stream_events_finalize_cleans_up_stream(monkeypatch) -> None:
    service = DealJobSSEService(uuid4(), uuid4(), uuid4())

    async def fake_get_job_status():
        return None, None, False

    class FakeStreams:
        def __init__(self):
            self.deleted_key = None
            self._sent = False

        async def xread(self, *_args, **_kwargs):
            if self._sent:
                return []
            self._sent = True
            return [("1-0", {"type": "finalize"})]

        async def delete_stream(self, stream_key: str):
            self.deleted_key = stream_key
            return True

        async def close(self):
            return None

    fake_streams = FakeStreams()
    monkeypatch.setattr(service, "_get_job_status", fake_get_job_status)
    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_sse_service.AsyncRedisStreams",
        lambda: fake_streams,
    )

    events: list[dict] = []
    async for event in service.stream_events(timeout_seconds=2, heartbeat_interval=0.1):
        events.append(event)

    assert events[0]["event"] == "ready"
    assert events[1]["event"] == "frame"
    assert events[2]["event"] == "complete"
    assert fake_streams.deleted_key == service.stream_key
