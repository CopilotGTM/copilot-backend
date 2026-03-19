from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.agents.common.stream_buffer import StreamingAnswerBuffer


@pytest.mark.asyncio
async def test_stream_yields_run_completed_event():
    """RunCompleted event should be captured and yielded after 'final'."""

    async def fake_response():
        yield SimpleNamespace(event=None, content="hello", tool=None)
        yield SimpleNamespace(event="RunCompleted", content=None, tool=None)

    buffer = StreamingAnswerBuffer([])
    results = []
    async for kind, content, json_started in buffer.stream(fake_response()):
        results.append((kind, content, json_started))

    kinds = [r[0] for r in results]
    assert "run_completed" in kinds
    final_idx = kinds.index("final")
    run_completed_idx = kinds.index("run_completed")
    assert run_completed_idx > final_idx


@pytest.mark.asyncio
async def test_stream_no_run_completed_when_absent():
    """No run_completed yield when RunCompleted event is missing."""

    async def fake_response():
        yield SimpleNamespace(event=None, content="hello", tool=None)

    buffer = StreamingAnswerBuffer([])
    results = []
    async for kind, content, json_started in buffer.stream(fake_response()):
        results.append((kind, content, json_started))

    kinds = [r[0] for r in results]
    assert "run_completed" not in kinds
