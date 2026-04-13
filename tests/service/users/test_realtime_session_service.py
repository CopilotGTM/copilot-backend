from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from src.domain.models.user_models import (
    RealtimeControlMode,
    RealtimeConversationMode,
    RealtimeDevicePlatform,
    RealtimeToolPolicy,
    UserRealtimeCallAttachRequest,
    UserRealtimeDeviceInfo,
    UserRealtimeRuntimeConfig,
    UserRealtimeSessionRequest,
)
from src.infra.auth.user_context import UserIDs
from src.service.users import realtime_session_service as realtime_module
from src.service.users.realtime_session_service import (
    RealtimeSessionService,
    RealtimeSessionServiceError,
    verify_runtime_config_signature,
)


class FakeAsyncClient:
    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.last_post: dict[str, object] | None = None

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(
        self, url: str, headers: dict[str, str], json: dict[str, object]
    ) -> httpx.Response:
        self.last_post = {
            "url": url,
            "headers": headers,
            "json": json,
        }
        if self.exc is not None:
            raise self.exc
        if self.response is None:
            raise AssertionError("FakeAsyncClient response was not configured")
        return self.response


class FakeOrgSession:
    def __init__(self):
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


def _make_user_ids() -> UserIDs:
    return UserIDs(
        user_id=uuid4(),
        organisation_id=uuid4(),
        user_name="Test User",
        user_email="test@example.com",
    )


def _make_request() -> UserRealtimeSessionRequest:
    return UserRealtimeSessionRequest(
        device=UserRealtimeDeviceInfo(
            platform=RealtimeDevicePlatform.ANDROID,
            app_version="1.0.0",
        ),
        conversation_mode=RealtimeConversationMode.PUSH_TO_TALK,
    )


@pytest.mark.asyncio
async def test_create_session_returns_signed_runtime_config(monkeypatch) -> None:
    fake_response = httpx.Response(
        200,
        request=httpx.Request(
            "POST", "https://example.openai.azure.com/openai/v1/realtime/client_secrets"
        ),
        json={
            "value": "ephemeral-secret",
            "expires_at": "2026-04-11T10:45:00Z",
            "session": {"id": "azure-session-123"},
        },
    )
    fake_client = FakeAsyncClient(response=fake_response)

    monkeypatch.setattr(realtime_module.httpx, "AsyncClient", lambda timeout: fake_client)
    monkeypatch.setattr(
        realtime_module.openai_config,
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(realtime_module.openai_config, "AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(realtime_module.agent_config, "VOICE_REALTIME_MODEL", "gpt-realtime-mini")
    monkeypatch.setattr(realtime_module.agent_config, "VOICE_REALTIME_VOICE", "alloy")
    monkeypatch.setattr(
        realtime_module,
        "_get_runtime_config_secret",
        lambda: "super-secret-signing-key",
    )

    async def fake_acquire_org_async_session(_organisation_id):
        yield FakeOrgSession()

    monkeypatch.setattr(
        realtime_module,
        "acquire_org_async_session",
        fake_acquire_org_async_session,
    )

    user_ids = _make_user_ids()
    service = RealtimeSessionService(user_ids)
    result = await service.create_session(_make_request())

    assert result.session_id == "azure-session-123"
    assert result.ephemeral_key == "ephemeral-secret"
    assert result.endpoint == "https://example.openai.azure.com/openai/v1/realtime/calls"
    assert result.model == "gpt-realtime-mini"
    assert result.voice == "alloy"
    assert result.turn_mode.value == "push_to_talk"
    assert result.expires_at == datetime(2026, 4, 11, 10, 45, 0, tzinfo=UTC)
    assert result.runtime_config.control_mode.value == "backend_sideband"
    assert result.runtime_config.tool_policy.value == "guardrailed_auto"
    assert "search_internal_knowledge" in result.runtime_config.allowed_tools
    assert "create_deal" not in result.runtime_config.allowed_tools
    assert result.runtime_config.session_update["type"] == "session.update"
    assert result.runtime_config.session_update["session"]["instructions"].startswith("You are Kai")
    assert result.runtime_config.session_update["session"]["tools"]
    assert result.runtime_config.session_update["session"]["tools"][0]["type"] == "function"
    assert result.runtime_config.session_update["session"]["tools"][0]["name"] in (
        result.runtime_config.allowed_tools
    )
    assert verify_runtime_config_signature(
        result.runtime_config,
        user_ids=user_ids,
        session_id=result.session_id,
        chat_session_id=result.chat_session_id,
        secret="super-secret-signing-key",
    )
    assert fake_client.last_post is not None
    assert fake_client.last_post["url"] == (
        "https://example.openai.azure.com/openai/v1/realtime/client_secrets"
    )
    assert fake_client.last_post["headers"]["api-key"] == "test-key"
    assert fake_client.last_post["json"]["session"]["model"] == "gpt-realtime-mini"
    assert fake_client.last_post["json"]["session"]["audio"]["output"]["voice"] == "alloy"
    assert "instructions" not in fake_client.last_post["json"]["session"]


def test_verify_runtime_config_signature_rejects_tampering() -> None:
    user_ids = _make_user_ids()
    runtime_config = realtime_module._build_runtime_config(
        session_id="azure-session-123",
        chat_session_id=str(uuid4()),
        voice="alloy",
        expires_at=datetime(2026, 4, 11, 10, 45, 0, tzinfo=UTC),
        user_ids=user_ids,
        secret="super-secret-signing-key",
        org_name="Acme Corp",
    )

    assert verify_runtime_config_signature(
        runtime_config,
        user_ids=user_ids,
        session_id="azure-session-123",
        chat_session_id=runtime_config.chat_session_id,
        secret="super-secret-signing-key",
    )

    runtime_config.allowed_tools.append("create_deal")

    assert not verify_runtime_config_signature(
        runtime_config,
        user_ids=user_ids,
        session_id="azure-session-123",
        chat_session_id=runtime_config.chat_session_id,
        secret="super-secret-signing-key",
    )


@pytest.mark.asyncio
async def test_create_session_raises_for_missing_config(monkeypatch) -> None:
    monkeypatch.setattr(realtime_module.openai_config, "AZURE_OPENAI_ENDPOINT", None)
    monkeypatch.setattr(realtime_module.openai_config, "AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(realtime_module.agent_config, "VOICE_REALTIME_MODEL", "gpt-realtime-mini")

    service = RealtimeSessionService(_make_user_ids())

    with pytest.raises(RealtimeSessionServiceError) as exc_info:
        await service.create_session(_make_request())

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_create_session_maps_upstream_http_errors(monkeypatch) -> None:
    request = httpx.Request(
        "POST", "https://example.openai.azure.com/openai/v1/realtime/client_secrets"
    )
    response = httpx.Response(401, request=request, json={"error": {"message": "Unauthorized"}})
    fake_client = FakeAsyncClient(
        response=None,
        exc=httpx.HTTPStatusError("boom", request=request, response=response),
    )

    monkeypatch.setattr(realtime_module.httpx, "AsyncClient", lambda timeout: fake_client)
    monkeypatch.setattr(
        realtime_module.openai_config,
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(realtime_module.openai_config, "AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(realtime_module.agent_config, "VOICE_REALTIME_MODEL", "gpt-realtime-mini")

    service = RealtimeSessionService(_make_user_ids())

    with pytest.raises(RealtimeSessionServiceError) as exc_info:
        await service.create_session(_make_request())

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_attach_call_rejects_tampered_runtime_config() -> None:
    user_ids = _make_user_ids()
    service = RealtimeSessionService(user_ids)
    runtime_config = UserRealtimeRuntimeConfig(
        chat_session_id=str(uuid4()),
        control_mode=RealtimeControlMode.BACKEND_SIDEBAND,
        tool_policy=RealtimeToolPolicy.GUARDED_AUTO,
        allowed_tools=["search_internal_knowledge"],
        session_update={"type": "session.update", "session": {"instructions": "You are Kai."}},
        issued_at=datetime(2099, 4, 11, 9, 45, 0, tzinfo=UTC),
        expires_at=datetime(2099, 4, 11, 10, 45, 0, tzinfo=UTC),
        signature="bad-signature",
    )

    with pytest.raises(RealtimeSessionServiceError) as exc_info:
        await service.attach_call(
            UserRealtimeCallAttachRequest(
                session_id="azure-session-123",
                call_id="call-123",
                chat_session_id=runtime_config.chat_session_id,
                runtime_config=runtime_config,
            )
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_attach_call_dispatches_worker_task(monkeypatch) -> None:
    user_ids = _make_user_ids()
    service = RealtimeSessionService(user_ids)
    chat_session_id = str(uuid4())
    runtime_config = realtime_module._build_runtime_config(
        session_id="azure-session-123",
        chat_session_id=chat_session_id,
        voice="alloy",
        expires_at=datetime(2099, 4, 11, 10, 45, 0, tzinfo=UTC),
        user_ids=user_ids,
        secret="super-secret-signing-key",
        org_name="Acme Corp",
    )
    attach_request = UserRealtimeCallAttachRequest(
        session_id="azure-session-123",
        call_id="call-123",
        chat_session_id=chat_session_id,
        runtime_config=runtime_config,
    )

    class FakeRedis:
        def __init__(self):
            self.store = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return False
            self.store[key] = value
            return True

        def get(self, key):
            return self.store.get(key)

        def delete(self, key):
            self.store.pop(key, None)

    dispatched: dict[str, object] = {}

    monkeypatch.setattr(
        realtime_module,
        "_get_runtime_config_secret",
        lambda: "super-secret-signing-key",
    )
    monkeypatch.setattr(realtime_module, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        realtime_module.openai_config,
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(realtime_module.openai_config, "AZURE_OPENAI_API_KEY", "test-key")

    async def fake_send_chat_task_async(
        task_name, args=None, kwargs=None, queue="chat_messages", routing_key=None, headers=None
    ):
        dispatched["task_name"] = task_name
        dispatched["args"] = args
        dispatched["queue"] = queue
        return "task-123"

    async def fake_load_owned_voice_chat_session(*, chat_session_id, db=None):
        return object()

    async def fake_store_call_attachment(
        *, chat_session, session_id, call_id, status, error=None, db=None
    ):
        dispatched["stored_status"] = status
        dispatched["stored_error"] = error
        dispatched["stored_call_id"] = call_id

    monkeypatch.setattr(realtime_module, "send_chat_task_async", fake_send_chat_task_async)
    monkeypatch.setattr(
        service, "_load_owned_voice_chat_session", fake_load_owned_voice_chat_session
    )
    monkeypatch.setattr(service, "_store_call_attachment", fake_store_call_attachment)

    result = await service.attach_call(attach_request)

    assert result.status == "queued"
    assert dispatched["task_name"] == "chat.voice_realtime_sideband"
    assert dispatched["queue"] == "voice_realtime"
    assert dispatched["stored_status"] == "queued"
    assert dispatched["args"][0] == str(user_ids.organisation_id)
    assert dispatched["args"][1] == str(user_ids.user_id)
    assert dispatched["args"][3] == chat_session_id
    assert dispatched["args"][5] == "call-123"
