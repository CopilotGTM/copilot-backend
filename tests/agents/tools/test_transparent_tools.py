"""Regression tests for transparent tool failure reporting."""

from __future__ import annotations

import pytest
from src.agents.tools.transparent_tools import TransparentToolManager


@pytest.mark.asyncio
async def test_handled_tool_error_marks_run_as_degraded() -> None:
    """A normal return that encodes failure must remain visible to telemetry."""

    frames = []
    manager = TransparentToolManager("message-1")

    async def fail_as_data() -> dict[str, str]:
        return {"status": "error", "message": "Meeting was not updated."}

    result = await manager.use_tool(
        "UpdateCalendarMeeting",
        fail_as_data,
        {},
        frame_emitter=frames.append,
    )

    assert result["status"] == "error"
    assert manager.had_tool_failure is True
    assert frames[-1].status == "completed"
    assert frames[-1].succeeded is False


@pytest.mark.asyncio
async def test_tool_exception_is_redacted_from_client_frame() -> None:
    """Provider exception details must stay in server logs, not streamed frames."""

    frames = []
    manager = TransparentToolManager("message-1")
    private_detail = "private attendee bhavesh@example.com"

    async def raise_private_error() -> None:
        raise RuntimeError(private_detail)

    with pytest.raises(RuntimeError, match="private attendee"):
        await manager.use_tool(
            "UpdateCalendarMeeting",
            raise_private_error,
            {},
            frame_emitter=frames.append,
        )

    assert manager.had_tool_failure is True
    assert frames[-1].status == "failed"
    assert frames[-1].result_summary == "Tool execution failed."
    assert private_detail not in str(frames[-1].model_dump())
