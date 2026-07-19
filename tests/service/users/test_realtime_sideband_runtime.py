from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
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
    def __init__(self, events: list[dict[str, object]] | None = None):
        self.events = events or []
        self.sent_events: list[dict[str, object]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_event(self, event: dict[str, object]) -> None:
        self.sent_events.append(event)

    async def iter_events(self) -> AsyncGenerator[dict[str, object], None]:
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


class FakeToolExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, tool_name: str, arguments: dict[str, object]) -> ToolExecutionResult:
        self.calls.append((tool_name, arguments))
        return ToolExecutionResult(ok=True, payload={"ok": True, "data": {"answer": "done"}})


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
async def test_backend_realtime_tool_executor_rejects_disallowed_tool() -> None:
    from src.agents.context import AgentContext

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
async def test_voice_realtime_sideband_service_passes_user_timezone_to_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeStateService:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def update_call_state(self, **_kwargs: object) -> None:
            return None

        def refresh_lock(self) -> None:
            return None

        def release_lock(self) -> None:
            return None

    class CapturingToolExecutor:
        def __init__(self, *, context, allowed_tools):
            captured["context"] = context
            captured["allowed_tools"] = allowed_tools

        async def execute(
            self, tool_name: str, arguments: dict[str, object]
        ) -> ToolExecutionResult:
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
        yield object()

    async def fake_load_organisation_runtime_context(_session, _organisation_id):
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
