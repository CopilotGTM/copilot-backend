"""Regression tests for non-answer events in the shared Agno stream buffer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agno.models.response import ToolExecution
from src.agents.common.stream_buffer import StreamingAnswerBuffer


@pytest.mark.asyncio
async def test_stream_yields_run_completed_event():
    """RunCompleted event should be captured and yielded after 'final'."""

    async def fake_response():
        """Emit one answer and the run-completion metrics event."""
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
        """Emit a stream without completion metrics."""
        yield SimpleNamespace(event=None, content="hello", tool=None)

    buffer = StreamingAnswerBuffer([])
    results = []
    async for kind, content, json_started in buffer.stream(fake_response()):
        results.append((kind, content, json_started))

    kinds = [r[0] for r in results]
    assert "run_completed" not in kinds


@pytest.mark.asyncio
async def test_stream_surfaces_content_free_tool_validation_failure() -> None:
    """Tool validation failures must remain observable without logging their arguments."""

    async def fake_response():
        """Emit the same validation-failure shape observed from Agno in production."""
        yield SimpleNamespace(
            event="ToolCallCompleted",
            content="private validation details",
            tool=ToolExecution(
                tool_name="update_calendar_meeting_tool",
                tool_call_error=True,
                result="2 validation errors for update_calendar_meeting_tool",
            ),
        )

    buffer = StreamingAnswerBuffer([])
    results = [result async for result in buffer.stream(fake_response())]

    failures = [content for kind, content, _ in results if kind == "tool_failure"]
    assert failures == [
        {
            "tool_name": "update_calendar_meeting_tool",
            "failure_kind": "argument_validation",
        }
    ]
    assert buffer.had_tool_failure is True
    assert "private validation details" not in str(results)
