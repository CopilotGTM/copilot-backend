from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from src.domain.models.organisation_tables.job_models import JobStatus
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


def _make_user_ids(*, user_timezone: str | None = None) -> UserIDs:
    return UserIDs(
        user_id=uuid4(),
        organisation_id=uuid4(),
        user_name="Test User",
        user_email="test@example.com",
        user_timezone=user_timezone,
    )


def _make_request() -> UserRealtimeSessionRequest:
    return UserRealtimeSessionRequest(
        device=UserRealtimeDeviceInfo(
            platform=RealtimeDevicePlatform.ANDROID,
            app_version="1.0.0",
        ),
        conversation_mode=RealtimeConversationMode.REALTIME,
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

    async def fake_runtime_secret() -> str:
        return "super-secret-signing-key"

    monkeypatch.setattr(realtime_module, "_get_runtime_config_secret", fake_runtime_secret)

    async def fake_acquire_org_async_session(_organisation_id):
        yield FakeOrgSession()

    monkeypatch.setattr(
        realtime_module,
        "acquire_org_async_session",
        fake_acquire_org_async_session,
    )

    async def fake_load_organisation_runtime_context(_session, _organisation_id):
        return "Acme", "Directional organisation context"

    monkeypatch.setattr(
        realtime_module,
        "load_organisation_runtime_context",
        fake_load_organisation_runtime_context,
    )

    @asynccontextmanager
    async def fake_master_async_session_scope():
        yield object()

    monkeypatch.setattr(
        realtime_module,
        "master_async_session_scope",
        fake_master_async_session_scope,
    )

    user_ids = _make_user_ids()
    service = RealtimeSessionService(user_ids)
    result = await service.create_session(_make_request())

    assert result.session_id == "azure-session-123"
    assert result.ephemeral_key == "ephemeral-secret"
    assert result.endpoint == "https://example.openai.azure.com/openai/v1/realtime/calls"
    assert result.model == "gpt-realtime-mini"
    assert result.voice == "alloy"
    assert result.turn_mode.value == "server_vad"
    assert result.expires_at == datetime(2026, 4, 11, 10, 45, 0, tzinfo=UTC)
    assert result.runtime_config.control_mode.value == "backend_sideband"
    assert result.runtime_config.tool_policy.value == "guardrailed_auto"
    assert "search_internal_knowledge" in result.runtime_config.allowed_tools
    assert "create_deal" in result.runtime_config.allowed_tools
    assert "create_task" in result.runtime_config.allowed_tools
    assert result.runtime_config.session_update["type"] == "session.update"
    assert result.runtime_config.session_update["session"]["type"] == "realtime"
    assert (
        "Directional organisation context"
        not in result.runtime_config.session_update["session"]["instructions"]
    )
    assert result.runtime_config.organisation_context_item
    assert (
        result.runtime_config.organisation_context_item["item"]["content"][0]["text"]
        == "Directional organisation context"
    )
    assert "modalities" not in result.runtime_config.session_update["session"]
    assert result.runtime_config.session_update["session"]["output_modalities"] == ["audio"]
    assert result.runtime_config.session_update["session"]["instructions"].startswith("You are Kai")
    assert result.runtime_config.session_update["session"]["tools"]
    assert result.runtime_config.session_update["session"]["tools"][0]["type"] == "function"
    assert result.runtime_config.session_update["session"]["tools"][0]["name"] in (
        result.runtime_config.allowed_tools
    )
    assert result.runtime_config.session_update["session"]["audio"]["input"]["format"] == {
        "type": "audio/pcm",
        "rate": 24000,
    }
    assert result.runtime_config.session_update["session"]["audio"]["output"]["format"] == {
        "type": "audio/pcm",
        "rate": 24000,
    }
    assert result.runtime_config.session_update["session"]["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "silence_duration_ms": 700,
        "prefix_padding_ms": 300,
    }
    assert "turn_detection" not in result.runtime_config.session_update["session"]
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
        conversation_mode=RealtimeConversationMode.REALTIME,
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
async def test_get_runtime_config_secret_requires_configured_secret(monkeypatch) -> None:
    async def missing_secret() -> str:
        raise RuntimeError("missing secret")

    monkeypatch.setattr(
        realtime_module.security_secrets_config,
        "get_voice_realtime_config_secret",
        missing_secret,
    )

    with pytest.raises(RealtimeSessionServiceError) as exc_info:
        await realtime_module._get_runtime_config_secret()

    assert exc_info.value.status_code == 500


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
async def test_create_session_requires_runtime_signing_secret(monkeypatch) -> None:
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

    async def missing_runtime_secret() -> str:
        raise RealtimeSessionServiceError("missing runtime secret", 500)

    monkeypatch.setattr(realtime_module, "_get_runtime_config_secret", missing_runtime_secret)

    service = RealtimeSessionService(_make_user_ids())

    with pytest.raises(RealtimeSessionServiceError) as exc_info:
        await service.create_session(_make_request())

    assert exc_info.value.status_code == 500


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
    user_ids = _make_user_ids(user_timezone="Asia/Kolkata")
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
        conversation_mode=RealtimeConversationMode.REALTIME,
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
    created_job_id = uuid4()

    async def fake_runtime_secret() -> str:
        return "super-secret-signing-key"

    monkeypatch.setattr(realtime_module, "_get_runtime_config_secret", fake_runtime_secret)
    monkeypatch.setattr(realtime_module, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        realtime_module.openai_config,
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(realtime_module.openai_config, "AZURE_OPENAI_API_KEY", "test-key")

    async def fake_send_task_async(
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

    async def fake_create_voice_job(*, chat_session_id, session_id, call_id, db=None):
        dispatched["job_chat_session_id"] = chat_session_id
        dispatched["job_session_id"] = session_id
        dispatched["job_call_id"] = call_id

        class FakeJob:
            job_id = created_job_id

        return FakeJob()

    monkeypatch.setattr(realtime_module, "send_task_async", fake_send_task_async)
    monkeypatch.setattr(
        service, "_load_owned_voice_chat_session", fake_load_owned_voice_chat_session
    )
    monkeypatch.setattr(service, "_store_call_attachment", fake_store_call_attachment)
    monkeypatch.setattr(service, "_create_voice_job", fake_create_voice_job)

    result = await service.attach_call(attach_request)

    assert result.status == "queued"
    assert dispatched["task_name"] == "chat.voice_realtime_sideband"
    assert dispatched["queue"] == "voice_realtime"
    assert dispatched["stored_status"] == "queued"
    assert dispatched["args"][0] == str(user_ids.organisation_id)
    assert dispatched["args"][1] == str(user_ids.user_id)
    assert dispatched["args"][3] == chat_session_id
    assert dispatched["args"][5] == "call-123"
    assert dispatched["args"][7] == str(created_job_id)
    assert dispatched["args"][8] == "Asia/Kolkata"
    assert dispatched["job_chat_session_id"] == chat_session_id
    assert dispatched["job_session_id"] == "azure-session-123"
    assert dispatched["job_call_id"] == "call-123"


@pytest.mark.asyncio
async def test_attach_call_marks_created_job_failed_when_dispatch_fails(monkeypatch) -> None:
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
        conversation_mode=RealtimeConversationMode.REALTIME,
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

    created_job_id = uuid4()
    observed: dict[str, object] = {}

    async def fake_runtime_secret() -> str:
        return "super-secret-signing-key"

    monkeypatch.setattr(realtime_module, "_get_runtime_config_secret", fake_runtime_secret)
    monkeypatch.setattr(realtime_module, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(
        realtime_module.openai_config,
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(realtime_module.openai_config, "AZURE_OPENAI_API_KEY", "test-key")

    async def fake_send_task_async(*args, **kwargs):
        raise RuntimeError("dispatch failed")

    async def fake_load_owned_voice_chat_session(*, chat_session_id, db=None):
        return object()

    async def fake_store_call_attachment(
        *, chat_session, session_id, call_id, status, error=None, db=None
    ):
        observed.setdefault("stored_statuses", []).append(status)
        observed.setdefault("stored_errors", []).append(error)

    async def fake_create_voice_job(*, chat_session_id, session_id, call_id, db=None):
        class FakeJob:
            job_id = created_job_id

        return FakeJob()

    async def fake_update_voice_job(
        *, job_id, status, error_message=None, db=None, metadata_updates=None
    ):
        observed["failed_job_id"] = job_id
        observed["failed_job_status"] = status
        observed["failed_job_error"] = error_message
        observed["failed_job_metadata"] = metadata_updates

    monkeypatch.setattr(realtime_module, "send_task_async", fake_send_task_async)
    monkeypatch.setattr(
        service, "_load_owned_voice_chat_session", fake_load_owned_voice_chat_session
    )
    monkeypatch.setattr(service, "_store_call_attachment", fake_store_call_attachment)
    monkeypatch.setattr(service, "_create_voice_job", fake_create_voice_job)
    monkeypatch.setattr(service, "_update_voice_job", fake_update_voice_job)

    with pytest.raises(RealtimeSessionServiceError) as exc_info:
        await service.attach_call(attach_request)

    assert exc_info.value.status_code == 502
    assert observed["failed_job_id"] == created_job_id
    assert observed["failed_job_status"] == JobStatus.FAILED
    assert "dispatch failed" in observed["failed_job_error"]
