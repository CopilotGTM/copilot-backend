from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from src.agents.workflows.general.agent import GeneralAgent
from src.infra.messaging.chat_stream_models import AnswerDelta, TokenUsageFrame


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


class _FakeBufferWithRunCompleted:
    async def stream(self, _chat_response):
        model_metric = SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            reasoning_tokens=0,
            cache_read_tokens=20,
            cache_write_tokens=5,
            cost=0.005,
            id="gpt-4o",
            provider="azure_openai",
        )
        metrics = SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            reasoning_tokens=0,
            cache_read_tokens=20,
            cache_write_tokens=5,
            cost=0.005,
            duration=1.5,
            time_to_first_token=0.2,
            details={"model": [model_metric]},
        )
        run_event = SimpleNamespace(metrics=metrics, model=None)

        yield ("answer", "Hi", False)
        yield ("run_completed", run_event, False)
        yield ("final", "", False)


@pytest.mark.asyncio
async def test_run_completed_emits_token_usage_frame() -> None:
    agent = object.__new__(GeneralAgent)
    agent.capabilities = SimpleNamespace(message_id="msg-1")
    agent._frame_message_id = "msg-1"
    agent.json_extractor = None
    agent.metadata_parser = None

    async def _collect_frames():
        frames = []
        async for frame in agent._process_streaming_response(
            chat_response=None,
            buffer=_FakeBufferWithRunCompleted(),
            frame_queue=asyncio.Queue(),
        ):
            frames.append(frame)
        return frames

    frames = await asyncio.wait_for(_collect_frames(), timeout=2)

    token_frames = [f for f in frames if isinstance(f, TokenUsageFrame)]
    assert len(token_frames) == 1
    tf = token_frames[0]
    assert tf.input_tokens == 100
    assert tf.output_tokens == 50
    assert tf.cache_read_tokens == 20
    assert tf.cache_write_tokens == 5
    assert tf.model_name == "gpt-4o"
    assert tf.cost == 0.005


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
