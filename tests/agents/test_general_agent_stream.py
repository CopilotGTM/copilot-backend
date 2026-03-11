from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from src.agents.workflows.general.agent import GeneralAgent
from src.infra.messaging.chat_stream_models import AnswerDelta


class _FakeBuffer:
    async def stream(self, _chat_response):
        yield ("answer", "Hi", False)
        yield ("final", "", False)


@pytest.mark.asyncio
async def test_process_streaming_response_finishes_without_tool_frames() -> None:
    agent = object.__new__(GeneralAgent)
    agent.capabilities = SimpleNamespace(message_id="msg-1")
    agent._frame_message_id = "msg-1"

    async def _collect_frames():
        frames = []
        async for frame in agent._process_streaming_response(
            chat_response=None,
            buffer=_FakeBuffer(),
            frame_queue=asyncio.Queue(),
        ):
            frames.append(frame)
        return frames

    frames = await asyncio.wait_for(_collect_frames(), timeout=1)

    assert [frame.content for frame in frames if isinstance(frame, AnswerDelta)] == ["Hi"]


def test_get_frame_message_id_prefers_context_value() -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(message_id="assistant-123")
    agent._frame_message_id = "assistant-123"

    assert agent._get_frame_message_id() == "assistant-123"


def test_get_frame_message_id_is_stable_without_context_value() -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(message_id=None)
    agent._frame_message_id = None

    first = agent._get_frame_message_id()
    second = agent._get_frame_message_id()

    assert first == second
