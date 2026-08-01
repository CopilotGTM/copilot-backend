"""Integration contracts for realtime sideband events and backend tool execution."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.registry import ToolName
from src.domain.models.user_models import (
    RealtimeControlMode,
    RealtimeToolPolicy,
    UserRealtimeRuntimeConfig,
)
from src.service.users.voice_realtime_contract import build_voice_realtime_tool_schemas
from src.service.voice_realtime import runtime as runtime_module
from src.service.voice_realtime.runtime import (
    VoiceRealtimeSidebandRuntime,
    VoiceRealtimeSidebandService,
)
from src.service.voice_realtime.tool_executor import (
    BackendRealtimeToolExecutor,
    ToolExecutionResult,
)


class FakeTransport:
    """Capture provider events without opening an Azure WebSocket."""

    def __init__(self, events: list[dict[str, object]] | None = None):
        self.events = events or []
        self.sent_events: list[dict[str, object]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        """Record that the runtime opened its transport before sending events."""
        self.connected = True

    async def send_event(self, event: dict[str, object]) -> None:
        """Retain outbound events so tests can assert ordering and payloads."""
        self.sent_events.append(event)

    async def iter_events(self) -> AsyncGenerator[dict[str, object], None]:
        """Yield the provider events supplied by the current test."""
        for event in self.events:
            yield event

    async def close(self) -> None:
        """Record that the runtime closed the transport after its event loop."""
        self.closed = True


class FakeToolExecutor:
    """Capture provider-selected tool calls without invoking application services."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, tool_name: str, arguments: dict[str, object]) -> ToolExecutionResult:
        """Return a stable result while preserving the selected name and arguments."""
        self.calls.append((tool_name, arguments))
        return ToolExecutionResult(ok=True, payload={"ok": True, "data": {"answer": "done"}})


class ConfirmationToolExecutor:
    """Return a preview first and a sent receipt only for a later confirmed call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolExecutionResult:
        """Model the two tool outcomes needed to exercise the runtime guard."""
        self.calls.append((tool_name, arguments))
        status = "sent" if arguments.get("confirmed") is True else "confirmation_required"
        return ToolExecutionResult(ok=True, payload={"ok": True, "data": {"status": status}})


def _make_runtime_config() -> UserRealtimeRuntimeConfig:
    return UserRealtimeRuntimeConfig(
        chat_session_id=str(uuid4()),
        control_mode=RealtimeControlMode.BACKEND_SIDEBAND,
        tool_policy=RealtimeToolPolicy.GUARDED_AUTO,
        allowed_tools=["search_internal_knowledge"],
        session_update={
            "type": "session.update",
            "session": {
                "instructions": "You are Kai.",
                "tools": build_voice_realtime_tool_schemas(),
            },
        },
        organisation_context_item={
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Directional context"}],
            },
        },
        issued_at=datetime(2026, 4, 11, 9, 45, 0, tzinfo=UTC),
        expires_at=datetime(2026, 4, 11, 10, 45, 0, tzinfo=UTC),
        signature="signed-payload",
    )


@pytest.mark.asyncio
async def test_voice_realtime_sideband_runtime_handles_function_call_event() -> None:
    """A provider function event executes once and resumes generation with its output."""
    transport = FakeTransport(
        events=[
            {
                "type": "response.function_call_arguments.done",
                "name": "search_internal_knowledge",
                "call_id": "tool-call-123",
                "arguments": json.dumps({"query": "pricing"}),
            }
        ]
    )
    executor = FakeToolExecutor()
    runtime = VoiceRealtimeSidebandRuntime(
        runtime_config=_make_runtime_config(),
        transport=transport,
        tool_executor=executor,
    )

    await runtime.run()

    assert transport.connected
    assert transport.closed
    assert transport.sent_events[0]["type"] == "session.update"
    assert transport.sent_events[1]["type"] == "conversation.item.create"
    assert transport.sent_events[1]["item"]["role"] == "user"
    assert executor.calls == [("search_internal_knowledge", {"query": "pricing"})]
    assert transport.sent_events[2]["type"] == "conversation.item.create"
    assert transport.sent_events[2]["item"]["call_id"] == "tool-call-123"
    assert json.loads(transport.sent_events[2]["item"]["output"]) == {
        "ok": True,
        "data": {"answer": "done"},
    }
    assert transport.sent_events[3] == {"type": "response.create"}


@pytest.mark.asyncio
async def test_realtime_runtime_blocks_model_confirmation_in_the_preview_turn() -> None:
    """The model cannot approve its own external mutation within one user audio turn."""
    transport = FakeTransport(
        events=[
            {"type": "input_audio_buffer.committed", "item_id": "user-turn-1"},
            {
                "type": "response.function_call_arguments.done",
                "name": "send_email_draft",
                "call_id": "preview-call",
                "arguments": json.dumps({"draft_id": "draft-1", "confirmed": False}),
            },
            {
                "type": "response.function_call_arguments.done",
                "name": "send_email_draft",
                "call_id": "self-confirm-call",
                "arguments": json.dumps(
                    {
                        "draft_id": "draft-1",
                        "expected_content_hash": "hash-1",
                        "confirmed": True,
                    }
                ),
            },
        ]
    )
    executor = ConfirmationToolExecutor()
    runtime = VoiceRealtimeSidebandRuntime(
        runtime_config=_make_runtime_config(),
        transport=transport,
        tool_executor=executor,
    )

    await runtime.run()

    assert executor.calls == [("send_email_draft", {"draft_id": "draft-1", "confirmed": False})]
    blocked_output = json.loads(transport.sent_events[-2]["item"]["output"])
    assert blocked_output["error"]["code"] == "realtime_user_confirmation_required"


@pytest.mark.asyncio
async def test_realtime_runtime_allows_confirmation_after_new_user_audio() -> None:
    """One newly committed user response unlocks only the currently previewed action."""
    transport = FakeTransport(
        events=[
            {"type": "input_audio_buffer.committed", "item_id": "user-turn-1"},
            {
                "type": "response.function_call_arguments.done",
                "name": "send_email_draft",
                "call_id": "preview-call",
                "arguments": json.dumps({"draft_id": "draft-1", "confirmed": False}),
            },
            {"type": "input_audio_buffer.committed", "item_id": "user-turn-2"},
            {
                "type": "response.function_call_arguments.done",
                "name": "send_email_draft",
                "call_id": "confirmed-call",
                "arguments": json.dumps(
                    {
                        "draft_id": "draft-1",
                        "expected_content_hash": "hash-1",
                        "confirmed": True,
                    }
                ),
            },
        ]
    )
    executor = ConfirmationToolExecutor()
    runtime = VoiceRealtimeSidebandRuntime(
        runtime_config=_make_runtime_config(),
        transport=transport,
        tool_executor=executor,
    )

    await runtime.run()

    assert len(executor.calls) == 2
    assert executor.calls[-1][1]["confirmed"] is True
    sent_output = json.loads(transport.sent_events[-2]["item"]["output"])
    assert sent_output["data"]["status"] == "sent"


@pytest.mark.asyncio
async def test_backend_realtime_tool_executor_rejects_disallowed_tool() -> None:
    """The signed allowlist remains an execution boundary even for registered tools."""
    executor = BackendRealtimeToolExecutor(
        context=AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="web"),
        allowed_tools=["search_internal_knowledge"],
    )

    result = await executor.execute("create_deal", {"name": "Blocked"})

    assert result.ok is False
    assert result.payload == {
        "ok": False,
        "error": {
            "code": "tool_not_allowed",
            "message": "Tool 'create_deal' is not allowed.",
        },
    }


@pytest.mark.asyncio
async def test_backend_realtime_tool_executor_strips_bot_channel_metadata() -> None:
    """Provider function output keeps public reviews while hiding channel transport payloads."""
    executor = BackendRealtimeToolExecutor(
        context=AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="voice"),
        allowed_tools=[ToolName.SEARCH_INTERNAL_KNOWLEDGE.value],
    )
    executor.factories[ToolName.SEARCH_INTERNAL_KNOWLEDGE] = lambda: lambda: {
        "status": "review",
        "review_text": "Please review this action.",
        "_bot_channel_payload": {"type": "private-card"},
        "nested": {"_bot_channel_payload": {"type": "nested-private-card"}},
    }

    result = await executor.execute(ToolName.SEARCH_INTERNAL_KNOWLEDGE.value, {})

    assert result.ok is True
    assert result.payload["data"] == {
        "status": "review",
        "review_text": "Please review this action.",
        "nested": {},
    }


@pytest.mark.asyncio
async def test_backend_realtime_tool_executor_rejects_image_tool_results() -> None:
    """Raw image bytes never enter the JSON-only realtime function-output channel."""
    executor = BackendRealtimeToolExecutor(
        context=AgentContext(organisation_id=uuid4(), user_id=uuid4(), channel="voice"),
        allowed_tools=[ToolName.SEARCH_INTERNAL_KNOWLEDGE.value],
    )
    executor.factories[ToolName.SEARCH_INTERNAL_KNOWLEDGE] = lambda: lambda: SimpleNamespace(
        content="image",
        images=[object()],
    )

    result = await executor.execute(ToolName.SEARCH_INTERNAL_KNOWLEDGE.value, {})

    assert result.ok is True
    assert result.payload["data"] == {
        "status": "unsupported",
        "code": "voice_image_attachment_unsupported",
        "message": "Image attachments cannot be analyzed in the realtime voice session.",
    }


@pytest.mark.asyncio
async def test_voice_realtime_sideband_service_passes_user_timezone_to_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sideband tools receive the app-captured timezone for spoken local-time requests."""
    captured: dict[str, object] = {}

    class FakeStateService:
        """Avoid Redis state changes while exercising service context construction."""

        def __init__(self, **_kwargs: object) -> None:
            return None

        async def update_call_state(self, **_kwargs: object) -> None:
            """Accept lifecycle updates without writing shared state."""
            return None

        def refresh_lock(self) -> None:
            """Skip lock refresh because no concurrent worker exists in this test."""
            return None

        def release_lock(self) -> None:
            """Skip lock release because the fake never acquires a lock."""
            return None

    class CapturingToolExecutor:
        """Expose the context built by the service for assertions."""

        def __init__(self, *, context, allowed_tools):
            captured["context"] = context
            captured["allowed_tools"] = allowed_tools

        async def execute(
            self, tool_name: str, arguments: dict[str, object]
        ) -> ToolExecutionResult:
            """Provide the executor protocol without performing a tool call."""
            return ToolExecutionResult(ok=True, payload={"ok": True})

    monkeypatch.setattr(runtime_module, "VoiceRealtimeStateService", FakeStateService)
    monkeypatch.setattr(runtime_module, "BackendRealtimeToolExecutor", CapturingToolExecutor)
    monkeypatch.setattr(
        runtime_module.openai_config,
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(runtime_module.openai_config, "AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(runtime_module.agent_config, "VOICE_REALTIME_MODEL", "gpt-realtime-mini")

    @asynccontextmanager
    async def fake_master_async_session_scope():
        """Provide the organization lookup dependency without a database connection."""
        yield object()

    async def fake_load_organisation_runtime_context(_session, _organisation_id):
        """Return stable organization context for sideband construction."""
        return "Acme", "Directional organisation context"

    monkeypatch.setattr(
        runtime_module,
        "master_async_session_scope",
        fake_master_async_session_scope,
    )
    monkeypatch.setattr(
        runtime_module,
        "load_organisation_runtime_context",
        fake_load_organisation_runtime_context,
    )

    service = VoiceRealtimeSidebandService(
        organisation_id=uuid4(),
        user_id=uuid4(),
        user_email="test@example.com",
        chat_session_id=str(uuid4()),
        session_id="azure-session-123",
        call_id="call-123",
        runtime_config=_make_runtime_config(),
        user_timezone="Asia/Kolkata",
        transport_factory=lambda **_kwargs: FakeTransport(),
    )

    await service.run()

    context = captured["context"]
    assert context.metadata["user_timezone"] == "Asia/Kolkata"
