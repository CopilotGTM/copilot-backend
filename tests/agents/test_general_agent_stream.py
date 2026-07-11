from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from agno.media import Image
from agno.tools.function import ToolResult as AgnoToolResult
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


class _FakeBufferWithDuplicateFinalAnswer:
    async def stream(self, _chat_response):
        yield ("answer", "Hi - how can I help?", False)
        yield ("final", "\n\nHi - how can I help?", False)


@pytest.mark.asyncio
async def test_process_streaming_response_skips_replayed_final_answer() -> None:
    agent = object.__new__(GeneralAgent)
    agent.capabilities = SimpleNamespace(message_id="msg-1")
    agent._frame_message_id = "msg-1"

    async def _collect_frames():
        frames = []
        async for frame in agent._process_streaming_response(
            chat_response=None,
            buffer=_FakeBufferWithDuplicateFinalAnswer(),
            frame_queue=asyncio.Queue(),
        ):
            frames.append(frame)
        return frames

    frames = await asyncio.wait_for(_collect_frames(), timeout=1)

    assert [frame.content for frame in frames if isinstance(frame, AnswerDelta)] == [
        "Hi - how can I help?"
    ]


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


@pytest.mark.asyncio
async def test_ground_pending_attachments_adds_extracted_text(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})

    async def fake_read_chat_attachment(attachment_id, *, context, max_chars=12000):
        assert attachment_id == "doc-id"
        assert context is agent.context
        return {
            "filename": "doc.txt",
            "content_type": "text",
            "content": "file text",
            "truncated": False,
        }

    monkeypatch.setattr(
        "src.agents.workflows.general.agent.read_chat_attachment",
        fake_read_chat_attachment,
    )

    messages = [
        {
            "role": "user",
            "content": "",
            "attachment_manifest": "[Attached files]\n- id: doc-id\n  name: doc.txt",
        },
        {"role": "assistant", "content": "What should I do with it?"},
        {"role": "user", "content": "summarize it", "attachment_manifest": None},
    ]

    images, attachment_text = await agent._ground_pending_attachments(
        message_list=messages,
        images=None,
        attachment_text=None,
    )

    assert images is None
    assert "file text" in attachment_text


@pytest.mark.asyncio
async def test_ground_pending_attachments_adds_native_images(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})
    image = Image(content=b"image", format="png")

    async def fake_read_chat_attachment(_attachment_id, *, context, max_chars=12000):
        return AgnoToolResult(content="opened image", images=[image])

    monkeypatch.setattr(
        "src.agents.workflows.general.agent.read_chat_attachment",
        fake_read_chat_attachment,
    )

    messages = [
        {
            "role": "user",
            "content": "attached",
            "attachment_manifest": "[Attached files]\n- id: image-id\n  name: chart.png",
        },
        {"role": "assistant", "content": "What should I do with it?"},
        {"role": "user", "content": "what does it show?", "attachment_manifest": None},
    ]

    images, attachment_text = await agent._ground_pending_attachments(
        message_list=messages,
        images=None,
        attachment_text=None,
    )

    assert images == [image]
    assert "opened image" in attachment_text
