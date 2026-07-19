from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agno.media import Image
from src.agents.workflows.general.agent import GeneralAgent
from src.infra.messaging.chat_stream_models import AnswerDelta, TokenUsageFrame
from src.service.chat.chat_attachment_reader import AttachmentReadResult


class _FakeBuffer:
    async def stream(self, _chat_response):
        yield ("answer", "Hi", False)
        yield ("final", "", False)


@pytest.mark.asyncio
async def test_run_stream_logs_general_agent_latency_stages(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(channel="web", message_id="message-123")
    agent._frame_message_id = "message-123"
    agent.separators = ["separator"]
    latency_logger = MagicMock()

    async def _ground(**_kwargs):
        return None, None

    async def _stream(*_args, **_kwargs):
        yield AnswerDelta(content="Hi")

    agent._ground_automatic_attachments = _ground
    agent._process_streaming_response = _stream
    agent.general_agent = SimpleNamespace(arun=lambda **_kwargs: object())
    monkeypatch.setattr("src.agents.workflows.general.agent.logger", latency_logger)
    monkeypatch.setattr(
        "src.agents.workflows.general.agent.prepare_messages",
        lambda *_args, **_kwargs: ["private provider payload"],
    )

    frames = [frame async for frame in agent.run_stream([{"content": "private text"}])]

    assert any(isinstance(frame, AnswerDelta) for frame in frames)
    events = [
        call.kwargs["extra"]
        for call in latency_logger.info.call_args_list
        if call.kwargs.get("extra", {}).get("event") == "chat_agent_latency"
    ]
    assert [event["stage"] for event in events] == [
        "attachment_grounding",
        "message_preparation",
        "provider_time_to_first_frame",
    ]
    assert all(event["channel"] == "web" for event in events)
    assert all(event["message_id"] == "message-123" for event in events)
    assert all(event["outcome"] == "success" for event in events)
    assert all("content" not in event and "payload" not in event for event in events)


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
async def test_ground_automatic_attachments_adds_extracted_text(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})

    async def fake_read_chat_attachment(attachment_id, *, context, max_chars=12000):
        assert attachment_id == "doc-id"
        assert context is agent.context
        return AttachmentReadResult(
            attachment_id=attachment_id,
            filename="doc.txt",
            mimetype="text/plain",
            kind="text",
            text="file text",
        )

    monkeypatch.setattr(
        "src.agents.attachment_grounding.read_attachment_content",
        fake_read_chat_attachment,
    )

    messages = [
        {
            "role": "user",
            "content": "",
            "attachment_manifest": "[Attached files]\n- id: doc-id\n  name: doc.txt\n  type: text/plain",
        },
        {"role": "assistant", "content": "What should I do with it?"},
        {"role": "user", "content": "summarize it", "attachment_manifest": None},
    ]

    images, attachment_text = await agent._ground_automatic_attachments(
        message_list=messages,
        images=None,
        attachment_text=None,
    )

    assert images is None
    assert "file text" in attachment_text


@pytest.mark.asyncio
async def test_ground_automatic_attachments_adds_native_images(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})

    async def fake_read_chat_attachment(_attachment_id, *, context, max_chars=12000):
        return AttachmentReadResult(
            attachment_id=_attachment_id,
            filename="chart.png",
            mimetype="image/png",
            kind="image",
            content_bytes=b"image",
        )

    monkeypatch.setattr(
        "src.agents.attachment_grounding.read_attachment_content",
        fake_read_chat_attachment,
    )

    messages = [
        {
            "role": "user",
            "content": "attached",
            "attachment_manifest": "[Attached files]\n- id: image-id\n  name: chart.png\n  type: image/png",
        },
        {"role": "assistant", "content": "What should I do with it?"},
        {"role": "user", "content": "what does it show?", "attachment_manifest": None},
    ]

    images, attachment_text = await agent._ground_automatic_attachments(
        message_list=messages,
        images=None,
        attachment_text=None,
    )

    assert len(images) == 1
    assert images[0].id == "image-id"
    assert images[0].content == b"image"
    assert attachment_text is None


@pytest.mark.asyncio
async def test_ground_automatic_attachments_skips_audio(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})

    async def fail_read(*_args, **_kwargs):
        raise AssertionError("WhatsApp audio must not be transcribed twice")

    monkeypatch.setattr(
        "src.agents.attachment_grounding.read_attachment_content",
        fail_read,
    )

    messages = [
        {
            "role": "user",
            "content": "voice message",
            "attachment_manifest": (
                "[Attached files]\n- id: audio-id\n  name: voice.ogg\n  type: audio/ogg"
            ),
        }
    ]

    images, attachment_text = await agent._ground_automatic_attachments(
        message_list=messages,
        images=None,
        attachment_text=None,
    )

    assert images is None
    assert attachment_text is None


@pytest.mark.asyncio
async def test_ground_automatic_attachments_shares_one_text_budget(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})
    reads: list[str] = []

    async def fake_read(attachment_id, *, context, max_chars=12000):
        reads.append(attachment_id)
        return AttachmentReadResult(
            attachment_id=attachment_id,
            filename=f"{attachment_id}.txt",
            mimetype="text/plain",
            kind="text",
            text="x" * 200,
            truncated=True,
        )

    monkeypatch.setattr(
        "src.agents.attachment_grounding.read_attachment_content",
        fake_read,
    )
    monkeypatch.setattr(
        "src.agents.attachment_grounding.truncate_to_token_budget",
        lambda text, budget, _model: (
            text[:budget],
            len(text) > budget,
            min(len(text), budget),
        ),
    )
    monkeypatch.setattr(
        "src.agents.workflows.general.agent.chat_file_upload_config."
        "ATTACHMENT_AUTO_READ_MAX_TOKENS",
        80,
    )

    manifest = (
        "[Attached files]\n"
        "- id: one\n  name: one.txt\n  type: text/plain\n"
        "- id: two\n  name: two.txt\n  type: text/plain"
    )
    _images, attachment_text = await agent._ground_automatic_attachments(
        message_list=[
            {
                "role": "user",
                "content": "compare",
                "attachment_manifest": manifest,
            }
        ],
        images=None,
        attachment_text=None,
    )

    assert len(attachment_text) == 80
    assert reads == ["one"]


@pytest.mark.asyncio
async def test_ground_automatic_attachments_deduplicates_preloaded_image(monkeypatch) -> None:
    agent = object.__new__(GeneralAgent)
    agent.context = SimpleNamespace(metadata={})
    image = Image(id="image-id", content=b"image", format="png")

    async def fail_read(*_args, **_kwargs):
        raise AssertionError("image was already supplied by the caller")

    monkeypatch.setattr(
        "src.agents.attachment_grounding.read_attachment_content",
        fail_read,
    )

    images, _text = await agent._ground_automatic_attachments(
        message_list=[
            {
                "role": "user",
                "content": "inspect",
                "attachment_manifest": (
                    "[Attached files]\n- id: image-id\n  name: image.png\n  type: image/png"
                ),
            }
        ],
        images=[image],
        attachment_text=None,
    )

    assert images == [image]
